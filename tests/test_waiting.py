"""0.3 等待接口：长轮询、SSE 事件流、旧 hub 降级、退出码。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from calltable import cli
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


if __name__ == "__main__":
    unittest.main()
