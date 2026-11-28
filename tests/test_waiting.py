"""0.3 等待接口：长轮询、SSE 事件流、旧 hub 降级、退出码。"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from calltable import cli
from calltable import hub as hub_module
from calltable.client import HubClient, HubError
from calltable.db import Store
from calltable.hub import Hub, HubServer
from tests.helpers import make_config

TOKEN = "wait-token"


class WaitingHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.config = make_config(
            cls.root, hosts={
                "h1": {"slots": {"light": 1}},
                "h2": {"slots": {"light": 1}},
            },
            max_waiters=2, max_event_streams=2,
            event_heartbeat_seconds=0.2)
        cls.config.raw["token"] = TOKEN
        cls.config.token = TOKEN
        cls.hub = Hub(cls.config)
        cls.hub.store.seed_host("h1", {"light": 1}, [])
        cls.hub.store.seed_host("h2", {"light": 1}, [])
        cls.hub.store.heartbeat("h1", "seed", [])
        cls.hub.store.heartbeat("h2", "seed", [])
        cls.server = HubServer(("127.0.0.1", 0), __import__(
            "calltable.hub", fromlist=["Handler"]).Handler)
        cls.server.hub = cls.hub
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls._seq = 0

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.hub.close()
        cls.hub.store.close()
        cls.tmp.cleanup()

    def setUp(self):
        # 每个用例独立主机，避免前面的 running 任务占住 slot 互相污染。
        type(self)._seq += 1
        self.host = f"h{self._seq}"
        self.hub.store.seed_host(self.host, {"light": 1}, [])
        self.hub.store.heartbeat(self.host, f"seed-{self.host}", [])

    # ------------------------------------------------------------ helpers
    def _job(self, **kw):
        defaults = dict(project="demo", host=self.host, slot="light", locks=[],
                        cmd=["true"])
        defaults.update(kw)
        return self.hub.store.create_job(**defaults)

    def _get(self, path, timeout=10, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": "application/json",
                     **(headers or {})})
        return urllib.request.urlopen(request, timeout=timeout)

    def _open_stream(self, query="", headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/events{query}",
            headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})})
        # 上一个测试的流可能还没被服务端回收；503 时短暂重试。
        deadline = time.time() + 5
        while True:
            try:
                return urllib.request.urlopen(request, timeout=10)
            except urllib.error.HTTPError as exc:
                if exc.code != 503 or time.time() >= deadline:
                    raise
                time.sleep(0.05)

    @staticmethod
    def _read_event(response):
        event = data = None
        event_id = None
        while True:
            line = response.readline()
            if not line:
                return None
            text = line.decode("utf-8").rstrip("\n")
            if text.startswith("id: "):
                event_id = int(text[4:])
            elif text.startswith("event: "):
                event = text[7:]
            elif text.startswith("data: "):
                data = json.loads(text[6:])
            elif text == "" and event:
                return {"id": event_id, "event": event, "data": data}

    # ------------------------------------------------------------ 长轮询
    def test_wait_until_started_returns_when_the_job_starts(self):
        blocker = self._job()
        waiting = self._job()
        self.assertEqual(self.hub.store.claim(self.host, "w1")["id"], blocker["id"])

        result = {}

        def run():
            started = time.time()
            with self._get(f"/api/jobs/{waiting['id']}?wait=10&until=started") as resp:
                result["job"] = json.loads(resp.read())
                result["headers"] = dict(resp.headers)
            result["elapsed"] = time.time() - started

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.2)  # 让请求先挂起；即使先 claim 也只是提前满足条件
        self.hub.store.finish(blocker["id"], status="succeeded", exit_code=0)
        self.assertEqual(self.hub.store.claim(self.host, "w1")["id"], waiting["id"])
        thread.join(timeout=10)

        self.assertEqual(result["headers"].get("X-Calltable-Wait-Result"), "done")
        self.assertEqual(result["headers"].get("X-Calltable-Wait-Until"), "started")
        self.assertEqual(result["job"]["status"], "running")
        self.assertIsNotNone(result["job"]["started"])
        self.assertLess(result["elapsed"], 5)

    def test_wait_until_done_returns_satisfied_job(self):
        job = self._job()
        self.hub.store.claim(self.host, "w1")

        def finish_soon():
            time.sleep(0.2)
            self.hub.store.finish(job["id"], status="succeeded", exit_code=0)

        thread = threading.Thread(target=finish_soon)
        thread.start()
        with self._get(f"/api/jobs/{job['id']}?wait=10&until=done") as resp:
            payload = json.loads(resp.read())
            headers = dict(resp.headers)
        thread.join(timeout=10)
        self.assertEqual(headers["X-Calltable-Wait-Result"], "done")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["exit_code"], 0)

    def test_long_poll_times_out_and_says_so(self):
        job = self._job()
        started = time.time()
        with self._get(f"/api/jobs/{job['id']}?wait=1&until=done") as resp:
            payload = json.loads(resp.read())
            headers = dict(resp.headers)
        self.assertEqual(headers["X-Calltable-Wait-Result"], "timeout")
        self.assertEqual(payload["status"], "queued")
        self.assertGreaterEqual(time.time() - started, 0.7)

    def test_wait_rejects_unknown_until(self):
        job = self._job()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/jobs/{job['id']}?wait=1&until=soon")
        self.assertEqual(ctx.exception.code, 400)

    def test_wait_limit_returns_503_with_retry_after(self):
        job = self._job()
        self.assertTrue(self.hub.wait_slots.acquire(blocking=False))
        self.assertTrue(self.hub.wait_slots.acquire(blocking=False))
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(f"/api/jobs/{job['id']}?wait=5")
            self.assertEqual(ctx.exception.code, 503)
            self.assertEqual(ctx.exception.headers.get("Retry-After"), "1")
        finally:
            self.hub.wait_slots.release()
            self.hub.wait_slots.release()

    def test_heartbeat_refreshes_a_queued_hold(self):
        job = self._job(hold_seconds=60)
        with self._get(f"/api/jobs/{job['id']}/heartbeat", method="POST") as resp:
            payload = json.loads(resp.read())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["hold_seconds"], 60)
        refreshed = self.hub.store.job(job["id"])
        self.assertGreater(refreshed["last_hold"], refreshed["created"])

    # ------------------------------------------------------------ SSE
    def test_events_require_auth(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/events")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_event_stream_sends_keepalive_comments(self):
        with self._open_stream() as stream:
            self.assertTrue(stream.readline().decode().startswith("retry:"))
            self.assertEqual(stream.readline().decode().strip(), "")
            keepalive = stream.readline().decode().strip()
        self.assertEqual(keepalive, ": keepalive")

    def test_event_stream_limit_returns_503(self):
        # 前面的用例关掉的流可能还没被服务端回收：先等两个名额都空出来。
        deadline = time.time() + 5
        while True:
            first = self.hub.event_slots.acquire(blocking=False)
            second = self.hub.event_slots.acquire(blocking=False)
            if first and second:
                break
            if first:
                self.hub.event_slots.release()
            if second:
                self.hub.event_slots.release()
            if time.time() >= deadline:
                self.fail("event stream slots never became free")
            time.sleep(0.05)
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/events",
                headers={"Authorization": f"Bearer {TOKEN}"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 503)
            self.assertEqual(ctx.exception.headers.get("Retry-After"), "1")
        finally:
            self.hub.event_slots.release()
            self.hub.event_slots.release()

    def test_event_stream_delivers_events_and_resumes(self):
        job = self._job()
        with self._open_stream() as stream:
            self.assertEqual(stream.readline().decode().strip(), "retry: 1000")
            self.assertEqual(stream.readline().decode().strip(), "")
            self.assertEqual(self.hub.store.claim(self.host, "w1")["id"],
                             job["id"])
            started = self._read_event(stream)
        self.assertIsNotNone(started, "no job.started event")
        self.assertEqual(started["event"], "job.started")
        self.assertEqual(started["data"]["job"]["id"], job["id"])

        # 断线期间任务结束：用 Last-Event-ID 续传，只收到新事件。
        self.hub.store.finish(job["id"], status="succeeded", exit_code=0)
        with self._open_stream(headers={"Last-Event-ID": str(started["id"])}) as stream:
            stream.readline()  # retry:
            stream.readline()  # blank
            finished = self._read_event(stream)
        self.assertIsNotNone(finished, "no job.finished event after resume")
        self.assertEqual(finished["event"], "job.finished")
        self.assertGreater(finished["id"], started["id"])
        self.assertEqual(finished["data"]["job"]["status"], "succeeded")
        self.assertEqual(finished["data"]["event_id"], finished["id"])

    def test_event_stream_can_filter_one_job(self):
        first = self._job()
        second = self._job()
        with self._open_stream(query=f"?job={second['id']}") as stream:
            stream.readline()
            stream.readline()
            self.hub.store.claim(self.host, "w1")
            self.assertTrue(self.hub.store.request_cancel(first["id"]))
            self.hub.store.finish(second["id"], status="succeeded", exit_code=0)
            event = self._read_event(stream)
        self.assertIsNotNone(event)
        self.assertEqual(event["data"]["job"]["id"], second["id"])
        self.assertEqual(event["event"], "job.finished")


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "hub.sqlite3")
        self.store.seed_host("h1", {"light": 1}, [])
        self.store.heartbeat("h1", "seed", [])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _job(self, **kw):
        defaults = dict(project="demo", host="h1", slot="light", locks=[],
                        cmd=["true"])
        defaults.update(kw)
        return self.store.create_job(**defaults)

    def test_events_are_monotonic_and_survive_store_restart(self):
        job = self._job()
        self.store.claim("h1", "w1")
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        events = self.store.events_after(0)
        self.assertEqual([e["type"] for e in events],
                         ["job.queued", "job.started", "job.finished"])
        self.assertEqual([e["seq"] for e in events],
                         sorted(e["seq"] for e in events))
        self.store.close()
        reopened = Store(self.root / "hub.sqlite3")
        try:
            self.assertEqual(reopened.last_event_seq(), events[-1]["seq"])
            self.assertEqual(len(reopened.events_after(0)), 3)
        finally:
            reopened.close()
        self.store = Store(self.root / "hub.sqlite3")  # for tearDown

    def test_repeated_finish_reports_do_not_duplicate_events(self):
        job = self._job()
        self.store.claim("h1", "w1")
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        finished = [e for e in self.store.events_after(0)
                    if e["type"] == "job.finished"]
        self.assertEqual(len(finished), 1)

    def test_event_pruning_keeps_the_newest(self):
        for _ in range(5):
            self._job()
        self.assertGreaterEqual(self.store.last_event_seq(), 5)
        self.store.prune_events(keep=2)
        remaining = self.store.events_after(0)
        self.assertEqual(len(remaining), 2)
        self.assertEqual(self.store.last_event_seq(), remaining[-1]["seq"])

    def test_cancel_of_a_queued_job_emits_finished(self):
        job = self._job()
        self.store.request_cancel(job["id"])
        events = self.store.events_after(0)
        self.assertEqual(events[-1]["type"], "job.finished")
        self.assertEqual(events[-1]["data"]["job"]["status"], "cancelled")
        self.assertEqual(events[-1]["data"]["job"]["exit_code"], -2)

    def test_hold_reaper_cancels_queued_jobs_only(self):
        running = self._job()
        self.assertEqual(self.store.claim("h1", "w1")["id"], running["id"])
        held = self._job(hold_seconds=5)
        # 把 held 的 last_hold 推回过去
        with self.store.lock:
            self.store.conn.execute("UPDATE jobs SET last_hold=created-100 WHERE id=?",
                                    (held["id"],))
        reaped = self.store.reap_queue_holds()
        self.assertEqual([job["id"] for job in reaped], [held["id"]])
        self.assertEqual(self.store.job(held["id"])["status"], "cancelled")
        self.assertEqual(self.store.job(held["id"])["exit_code"], -2)
        self.assertIn("queue hold expired", self.store.job(held["id"])["error"])
        self.assertEqual(self.store.job(running["id"])["status"], "running")


class CliWaitFallbackTests(unittest.TestCase):
    """新客户端连旧 hub：没有 X-Calltable-Wait-Result 时退回轮询。"""

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.waits = 0

        def request(self, method, path, params=None, **kw):
            self.calls.append((method, path, dict(params or {})))
            if not params:  # 无 wait 的探测请求
                return {"id": "job-1", "status": "queued", "started": None,
                        "hold_seconds": 0, "host": "h1",
                        "slot": "light"}, {}
            self.waits += 1
            if self.waits > 1:  # 第二次长轮询时才结束
                return {"id": "job-1", "status": "succeeded", "exit_code": 7,
                        "started": 1.0, "finished": 2.0, "host": "h1",
                        "slot": "light"}, {}
            return {"id": "job-1", "status": "queued", "started": None,
                    "hold_seconds": 0, "host": "h1", "slot": "light"}, {}

        def post_json(self, path, body, **kw):
            return {"ok": True}

    def test_missing_wait_header_falls_back_to_polling(self):
        client = self.FakeClient()
        with mock.patch.object(cli.time, "sleep") as sleep:
            code = cli._follow(client, "job-1", timeout=30, until="done",
                               chunk=60, stream_logs=False)
        self.assertEqual(code, 7)
        # calls[0] 是无 wait 的探测请求；之后才是长轮询（旧 hub 忽略 wait）
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[0][2], {})
        self.assertEqual(client.calls[1][2].get("until"), "done")
        self.assertTrue(1 <= client.calls[1][2].get("wait") <= 60)
        sleep.assert_any_call(2.0)

    def test_local_timeout_uses_exit_code_75(self):
        class NeverDone:
            def request(self, method, path, params=None, **kw):
                return ({"id": "job-1", "status": "running", "started": 1.0,
                         "hold_seconds": 0, "host": "h1", "slot": "light"},
                        {"X-Calltable-Wait-Result": "timeout"})

            def post_json(self, path, body, **kw):
                return {}

        started = time.time()
        code = cli._follow(NeverDone(), "job-1", timeout=0.3, until="done",
                           chunk=1, stream_logs=False)
        self.assertEqual(code, cli.EXIT_WAIT_TIMEOUT)
        self.assertEqual(code, 75)
        self.assertLess(time.time() - started, 5)


class ClientErrorTests(unittest.TestCase):
    """读响应阶段的中断（socket 超时、对端不回就断开）统一成 status=None 的 HubError。"""

    def _client(self) -> HubClient:
        return HubClient("http://127.0.0.1:1", "t", timeout=5)

    def test_socket_timeout_is_a_hub_error(self):
        client = self._client()
        with mock.patch.object(client, "_open",
                               side_effect=socket.timeout("timed out")):
            with self.assertRaises(HubError) as ctx:
                client.request("GET", "/api/jobs/job-1", params={"wait": 60})
        self.assertIsNone(ctx.exception.status)
        # socket.timeout 就是 TimeoutError：消息里带类型名和原始说明。
        self.assertIn("TimeoutError", str(ctx.exception))
        self.assertIn("timed out", str(ctx.exception))

    def test_remote_disconnect_is_a_hub_error(self):
        client = self._client()
        with mock.patch.object(
                client, "_open",
                side_effect=http.client.RemoteDisconnected("closed")):
            with self.assertRaises(HubError) as ctx:
                client.request("GET", "/api/jobs/job-1")
        self.assertIsNone(ctx.exception.status)

    def test_long_poll_socket_timeout_leaves_room_for_the_hub(self):
        # WAIT_CHUNK 与 HubClient 的默认 timeout 都是 60s：不发明确超时的话，
        # 服务端整块等满的那一刻客户端正好抢跑。
        class Recorder:
            timeout = 60

            def __init__(self):
                self.calls = []

            def request(self, method, path, params=None, **kw):
                self.calls.append((dict(params or {}), kw.get("timeout")))
                return {"id": "job-1", "status": "queued"}, {}

        client = Recorder()
        cli._wait_call(client, "job-1", "done", chunk=cli.WAIT_CHUNK)
        params, timeout = client.calls[0]
        self.assertEqual(params["wait"], cli.WAIT_CHUNK)
        self.assertGreaterEqual(timeout, cli.WAIT_CHUNK + cli.WAIT_SOCKET_MARGIN)
        # 普通查询（chunk=0）不带 wait，也不强行拉长超时
        cli._wait_call(client, "job-1", "done", chunk=0)
        self.assertEqual(client.calls[1][0], {})
        self.assertIsNone(client.calls[1][1])

    def test_a_failing_log_fetch_does_not_end_the_wait(self):
        class FlakyLogs:
            def request(self, method, path, params=None, **kw):
                if path.endswith("/logs"):
                    raise HubError(f"{method} {path} -> RemoteDisconnected")
                return ({"id": "job-1", "status": "succeeded", "exit_code": 0,
                         "started": 1.0, "finished": 2.0, "host": "h1",
                         "slot": "light"},
                        {"X-Calltable-Wait-Result": "done"})

            def post_json(self, path, body, **kw):
                return {}

        with contextlib.redirect_stdout(io.StringIO()):
            code = cli._follow(FlakyLogs(), "job-1", timeout=10, until="done",
                               chunk=1, stream_logs=True)
        self.assertEqual(code, 0)


class CliWaitRetryTests(unittest.TestCase):
    """_follow 遇到 hub 瞬时错误：截止前退避重试、照常心跳，截止时仍出错才返回 1。"""

    class FlakyClient:
        def __init__(self, error: HubError):
            self.error = error
            self.waits = 0
            self.plain = 0
            self.beats = 0

        def request(self, method, path, params=None, **kw):
            if "wait" not in (params or {}):  # 探测请求 / 名额满后的普通查询
                self.plain += 1
                return {"id": "job-1", "status": "queued", "started": None,
                        "hold_seconds": 3, "host": "h1", "slot": "light"}, {}
            self.waits += 1
            raise self.error

        def post_json(self, path, body, **kw):
            self.beats += 1
            return {"ok": True}

    def _follow(self, client, timeout):
        started = time.time()
        with mock.patch.object(cli, "WAIT_RETRY_BASE", 0.05), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli._follow(client, "job-1", timeout=timeout, until="done",
                               chunk=60, stream_logs=False)
        return code, time.time() - started

    def test_transient_errors_retry_until_the_deadline_then_exit_1(self):
        for error in (HubError("GET /api/jobs/job-1 -> RemoteDisconnected"),
                      HubError("GET /api/jobs/job-1 -> HTTP 502", status=502)):
            with self.subTest(status=error.status):
                client = self.FlakyClient(error)
                code, elapsed = self._follow(client, timeout=2.2)
                # hub 一直出错到截止时间才返回 1（README/spec 的退出码契约）。
                self.assertEqual(code, 1)
                self.assertGreaterEqual(elapsed, 2.0)
                self.assertGreater(client.waits, 2)
                # hold=3s → 心跳间隔 1s：重试期间也要继续心跳。
                self.assertGreaterEqual(client.beats, 2)

    def test_old_hub_503_keeps_heartbeating_and_checks_state(self):
        client = self.FlakyClient(
            HubError("GET /api/jobs/job-1 -> HTTP 503", status=503))
        code, _ = self._follow(client, timeout=2.2)
        # 名额满不是 hub 错误：截止时按本地超时返回 75。
        self.assertEqual(code, cli.EXIT_WAIT_TIMEOUT)
        self.assertGreaterEqual(client.beats, 2)
        # 503 的轮次改用不带 wait 的查询检查终态（第一次是探测请求）。
        self.assertGreater(client.plain, 1)

    def test_ct_wait_started_during_a_hub_restart_keeps_waiting(self):
        class Restarting:
            def __init__(self):
                self.calls = 0

            def get_json(self, path, params=None, **kw):
                raise HubError(f"GET {path} -> ConnectionRefusedError")

            def request(self, method, path, params=None, **kw):
                if path.endswith("/logs"):
                    return b"", {}
                self.calls += 1
                if self.calls < 3:  # 探测请求和第一次长轮询时 hub 还没起来
                    raise HubError(f"GET {path} -> ConnectionRefusedError")
                return ({"id": "job-1", "status": "succeeded", "exit_code": 0,
                         "started": 1.0, "finished": 2.0, "host": "h1",
                         "slot": "light"},
                        {"X-Calltable-Wait-Result": "done"})

            def post_json(self, path, body, **kw):
                return {}

        client = Restarting()
        args = mock.Mock(id="job-1", until="done", timeout="10s")
        with mock.patch.object(cli, "_client", return_value=client), \
                mock.patch.object(cli, "WAIT_RETRY_BASE", 0.05), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.cmd_wait(None, args)
        self.assertEqual(code, 0)
        self.assertEqual(client.calls, 3)


class FakeClock:
    """虚拟时钟：sleep 和长轮询只推进读数，测试不用真的等。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class CliWaitScheduleTests(unittest.TestCase):
    """心跳节奏与截止时间：用虚拟时钟跑完整个等待。"""

    class QueuedForever:
        """任务一直排队；长轮询等满 wait 才回包（推进虚拟时钟）。"""

        def __init__(self, clock: FakeClock, hold: int, fail_first: int = 0,
                     timeout: int = 3600):
            self.clock, self.hold, self.timeout = clock, hold, timeout
            self.fail_first = fail_first
            self.calls = 0
            self.beats: list[float] = []

        def get_json(self, path, params=None, **kw):
            return self.request("GET", path, params=params, **kw)[0]

        def request(self, method, path, params=None, **kw):
            self.calls += 1
            if self.calls <= self.fail_first:
                raise HubError(f"{method} {path} -> ConnectionRefusedError")
            if path.endswith("/logs"):
                return b"", {}
            wait = (params or {}).get("wait")
            if wait:
                self.clock.sleep(wait)
            job = {"id": "job-1", "status": "queued", "started": None,
                   "hold_seconds": self.hold, "ahead": 0, "host": "h1",
                   "slot": "light", "timeout": self.timeout}
            return job, ({"X-Calltable-Wait-Result": "timeout"} if wait else {})

        def post_json(self, path, body, **kw):
            self.beats.append(self.clock.time())
            return {"ok": True}

    class Unreachable:
        """hub 从来连不上（URL 配错、DNS 解析失败等，都是 status=None）。"""

        def __init__(self):
            self.calls = 0

        def get_json(self, path, params=None, **kw):
            return self.request("GET", path, params=params, **kw)[0]

        def request(self, method, path, params=None, **kw):
            self.calls += 1
            raise HubError(f"{method} {path} -> [Errno 8] nodename nor "
                           "servname provided, or not known")

        def post_json(self, path, body, **kw):
            raise HubError(f"POST {path} -> [Errno 8] nodename nor servname")

    def _quiet(self, clock: FakeClock):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cli, "time", clock))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return stack

    @staticmethod
    def _gaps(beats: list[float]) -> list[float]:
        return [b - a for a, b in zip(beats, beats[1:])]

    def test_heartbeats_keep_a_third_of_the_hold_between_them(self):
        # hold/3 不是整数时，长轮询 int(hold/3) 会比心跳到期早一点结束；
        # 不能因此隔一轮才心跳（实际间隔 2×hold/3）。
        clock = FakeClock()
        client = self.QueuedForever(clock, hold=10)
        with self._quiet(clock):
            code = cli._follow(client, "job-1", timeout=60, until="done",
                               chunk=cli.WAIT_CHUNK, stream_logs=False)
        self.assertEqual(code, cli.EXIT_WAIT_TIMEOUT)
        self.assertLessEqual(max(self._gaps(client.beats)), 10 / 3 + 0.01)
        self.assertGreaterEqual(len(client.beats), 15)

    def test_short_chunks_do_not_heartbeat_every_round(self):
        # `ct run --wait` 用 5s 的块；默认 hold 180s 时心跳仍约每 60s 一次。
        clock = FakeClock()
        client = self.QueuedForever(clock, hold=180)
        with self._quiet(clock):
            cli._follow(client, "job-1", timeout=600, until="done",
                        chunk=cli.RUN_WAIT_CHUNK, stream_logs=False)
        gaps = self._gaps(client.beats)
        self.assertLessEqual(max(gaps), 60 + 0.01)
        self.assertGreaterEqual(min(gaps), 50)

    def test_default_deadline_follows_the_job_timeout_after_a_failed_lookup(self):
        # ct wait 的首次查询赶上 hub 重启：截止时间不能固定成 3600+300，
        # 拿到任务后要按任务自己的 timeout + 5 分钟算。
        clock = FakeClock()
        client = self.QueuedForever(clock, hold=0, fail_first=1,
                                    timeout=7200)
        args = mock.Mock(id="job-1", until="done", timeout=None)
        started = clock.time()
        with self._quiet(clock), \
                mock.patch.object(cli, "_client", return_value=client):
            code = cli.cmd_wait(None, args)
        elapsed = clock.time() - started
        self.assertEqual(code, cli.EXIT_WAIT_TIMEOUT)
        self.assertGreaterEqual(elapsed, 7200 + 300 - 1)
        self.assertLess(elapsed, 7200 + 300 + cli.WAIT_CHUNK + 1)

    def test_a_long_outage_does_not_overflow_the_backoff(self):
        # 连续失败上千次时，2 ** (failures - 1) 会在 min() 之前就溢出成
        # OverflowError（长停机 + 很短的 sleep 很容易跑过这个点）：
        # 必须先给指数封顶。
        clock = FakeClock()
        client = self.Unreachable()
        job = {"id": "job-1", "status": "queued", "hold_seconds": 0,
               "timeout": 86400}
        with self._quiet(clock):
            code = cli._follow(client, "job-1", timeout=None, until="done",
                               chunk=cli.WAIT_CHUNK, stream_logs=False,
                               job=job)
        self.assertEqual(code, 1)
        self.assertGreater(client.calls, 1200)  # 确实跑过了指数溢出点

    def test_a_hub_that_never_answers_ends_the_wait_with_1_in_minutes(self):
        # 修复前：首次查询失败立即返回 1。不能变成默默阻塞 65 分钟。
        clock = FakeClock()
        client = self.Unreachable()
        args = mock.Mock(id="job-1", until="done", timeout=None)
        started = clock.time()
        with self._quiet(clock), \
                mock.patch.object(cli, "_client", return_value=client):
            code = cli.cmd_wait(None, args)
        elapsed = clock.time() - started
        self.assertEqual(code, 1)
        self.assertGreater(client.calls, 3)  # 期间确实在重试
        self.assertLessEqual(elapsed, 10 * 60)


class WaitResilienceTests(unittest.TestCase):
    """真实 hub + 真实 HubClient：断连和 5xx 都不会结束 ct wait。"""

    def _start_hub(self, **overrides) -> Hub:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.config = make_config(root, hosts={"h1": {"slots": {"light": 1}}},
                                  **overrides)
        hub = Hub(self.config)
        self.addCleanup(hub.store.close)
        self.addCleanup(hub.close)
        hub.store.heartbeat("h1", "seed", [])
        server = HubServer(("127.0.0.1", 0), hub_module.Handler)
        server.hub = hub
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.hub = hub
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        return hub

    def test_wait_survives_a_dropped_connection_and_5xx(self):
        hub = self._start_hub(max_wait_seconds=2)
        job = hub.store.create_job(project="demo", host="h1", slot="light",
                                   locks=[], cmd=["true"])
        hub.store.claim("h1", "w1")
        hub.store.append_log(job["id"], b"hello from the job\n")
        real = hub_module.Handler._job_detail
        calls = {"wait": 0}
        lock = threading.Lock()

        def flaky(handler, job_id):
            if not (handler.query.get("wait") or [""])[0]:
                return real(handler, job_id)
            with lock:
                calls["wait"] += 1
                n = calls["wait"]
            if n == 1:
                # hub 进程在长轮询中途退出：不回任何响应就断开连接。
                handler.close_connection = True
                return None
            if n == 2:
                # 重启中的反向代理：5xx；期间任务结束。
                hub.store.finish(job["id"], status="failed", exit_code=5)
                return handler._error(502, "bad gateway")
            return real(handler, job_id)

        client = HubClient(self.url, self.config.token_value, timeout=10)
        with mock.patch.object(hub_module.Handler, "_job_detail", flaky), \
                mock.patch.object(cli, "WAIT_RETRY_BASE", 0.05):
            # 先确认断连确实被包装成 status=None 的 HubError。
            with self.assertRaises(HubError) as ctx:
                client.request("GET", f"/api/jobs/{job['id']}",
                               params={"wait": 1})
            self.assertIsNone(ctx.exception.status)
            with lock:
                calls["wait"] = 0
            result: dict = {}
            err = io.StringIO()

            def run():
                try:
                    result["code"] = cli._follow(client, job["id"],
                                                 timeout=10, until="done",
                                                 chunk=2, stream_logs=True)
                except BaseException as exc:  # 记录下来，由主线程断言
                    result["exc"] = exc

            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "ct wait hung")
        self.assertNotIn("exc", result, err.getvalue())
        self.assertEqual(result.get("code"), 5, err.getvalue())
        self.assertGreaterEqual(calls["wait"], 3)


if __name__ == "__main__":
    unittest.main()
