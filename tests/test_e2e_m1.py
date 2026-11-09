"""0.3 端到端：取号回执 → 长轮询等待 → 签名 webhook → 重试 / 幂等 / 心跳。

本地起 hub + executor，随机端口，127.0.0.1 上的接收端；不碰任何真实部署。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "e2e-m1-token"
RAW_SECRET = b"e2e-standard-webhooks-secret"
WEBHOOK_SECRET = "whsec_" + base64.b64encode(RAW_SECRET).decode()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


def job_id_from(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("queued "):
            return line.split()[1]
        if line.startswith("already queued "):
            return line.split()[2]
    raise AssertionError("no queued job in output: " + stdout)


class Receiver:
    """记录 webhook 的本地接收端；按事件类型前 N 次返回 500 以测试重试。"""

    def __init__(self, fail_started: int = 0, fail_finished: int = 0,
                 default: int = 204):
        self.fail = {"job.started": fail_started,
                     "job.finished": fail_finished}
        self.counts: dict[str, int] = {}
        self.default = default
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer.lock:
                    outer.requests.append({
                        "headers": {key.lower(): value
                                    for key, value in self.headers.items()},
                        "body": body,
                    })
                    try:
                        kind = json.loads(body)["type"]
                    except (ValueError, KeyError, TypeError):
                        kind = ""
                    seen = outer.counts.get(kind, 0)
                    outer.counts[kind] = seen + 1
                    status = (500 if seen < outer.fail.get(kind, 0)
                              else outer.default)
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def events(self) -> list[dict]:
        with self.lock:
            raw = [json.loads(item["body"]) for item in self.requests]
        return raw


class EndToEndM1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(cls.tmp.name)
        cls.root = root
        cls.port = free_port()
        cls.repo = root / "demo"
        cls.repo.mkdir()
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "e2e@example.com")
        git(cls.repo, "config", "user.name", "e2e")
        (cls.repo / "README.md").write_text("demo\n", encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-qm", "init")

        profiles = root / "profiles"
        profiles.mkdir()
        (profiles / "demo@testhost.json").write_text(json.dumps({
            "name": "demo@testhost", "slot": "light", "artifacts": [],
            "timeout": "2m"}), encoding="utf-8")

        cls.config_path = root / "config.json"
        cls.config_path.write_text(json.dumps({
            "token": TOKEN,
            "data_dir": str(root / "data"),
            "profiles_dir": str(profiles),
            "hub_url": f"http://127.0.0.1:{cls.port}",
            "hub_bind": "127.0.0.1",
            "hub_port": cls.port,
            "public_url": f"http://127.0.0.1:{cls.port}",
            "hosts": {"testhost": {"slots": {"light": 2}},
                      "idlehost": {"slots": {"light": 1}}},
            "exec_host": "testhost",
            "runs_root": str(root / "runs"),
            "mirror_root": str(root / "mirrors"),
            "max_jobs": 2,
            "poll_interval": 0.3,
            "sample_interval": 1,
            "janitor_interval": 0.3,
            "use_systemd": False,
            "webhook_secret": WEBHOOK_SECRET,
            "webhook_timeout": 2,
            "webhook_retry_base_seconds": 0.2,
            "webhook_max_delay_seconds": 1,
            "webhook_max_attempts": 5,
            "event_heartbeat_seconds": 1,
        }, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        cls.env = env
        cls.hub_log = (root / "hub.log").open("w", encoding="utf-8")
        cls.hub = subprocess.Popen(
            [sys.executable, "-m", "calltable", "hub", "--config",
             str(cls.config_path)],
            cwd=str(REPO_ROOT), env=env, stdout=cls.hub_log,
            stderr=subprocess.STDOUT)
        cls._wait_health()

        cls.exec_log = (root / "exec.log").open("w", encoding="utf-8")
        cls.executor = subprocess.Popen(
            [sys.executable, "-m", "calltable", "exec", "--config",
             str(cls.config_path)],
            cwd=str(REPO_ROOT), env=env, stdout=cls.exec_log,
            stderr=subprocess.STDOUT)
        cls._wait_host_online()

    @classmethod
    def tearDownClass(cls):
        for proc in (getattr(cls, "executor", None), getattr(cls, "hub", None)):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for handle in (getattr(cls, "exec_log", None),
                       getattr(cls, "hub_log", None)):
            if handle:
                handle.close()
        cls.tmp.cleanup()

    # ------------------------------------------------------------ helpers
    @classmethod
    def _api(cls, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None

    @classmethod
    def _wait_health(cls, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{cls.port}/api/health", timeout=2):
                    return
            except (urllib.error.URLError, OSError):
                if cls.hub.poll() is not None:
                    raise RuntimeError("hub exited early")
                time.sleep(0.2)
        raise RuntimeError("hub did not become healthy")

    @classmethod
    def _wait_host_online(cls, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for host in cls._api("/api/state")["hosts"]:
                if host["name"] == "testhost" and host["online"]:
                    return
            time.sleep(0.3)
        raise RuntimeError("executor did not heartbeat")

    def _cli(self, *args, timeout=180):
        # --config 必须放在子命令之后、``--`` 之前；放在最后会被 argparse
        # 的 REMAINDER 当成任务命令的一部分。
        command = list(args)
        if command:
            command = [command[0], "--config", str(self.config_path),
                       *command[1:]]
        return subprocess.run(
            [sys.executable, "-m", "calltable", "cli", *command],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=timeout)

    def _submit(self, *extra, label="", command=None, wait=False):
        command = command or [sys.executable.replace("\\", "/"), "-c",
                              "print('m1-ok')"]
        args = ["run", "--host", "testhost", "--project", "demo",
                "--repo", str(self.repo)]
        if label:
            args += ["--label", label]
        if wait:
            args.append("--wait")
        args += list(extra)
        args += ["--", *command]
        result = self._cli(*args)
        return result

    def _wait_status(self, job_id, statuses, timeout=60):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            job = self._api(f"/api/jobs/{job_id}")
            if job["status"] in statuses:
                return job
            time.sleep(0.2)
        self.fail(f"job {job_id} stuck in {job and job['status']}: "
                  f"{job and job.get('error')}")

    def _wait_until(self, predicate, timeout=20, message="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail(f"timed out waiting for {message}")

    def _cancel(self, job_id):
        try:
            self._api(f"/api/jobs/{job_id}/cancel", method="POST")
        except urllib.error.HTTPError:
            pass

    def _drain_running(self):
        for job in self._api("/api/state")["running"]:
            self._cancel(job["id"])
        self._wait_until(
            lambda: not self._api("/api/state")["running"], timeout=30,
            message="running jobs to drain")

    # ------------------------------------------------------------ 兼容性
    def test_legacy_payload_and_plain_get_are_unchanged(self):
        # 用一个没有 executor 的主机，任务不会跑，状态断言是确定的。
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/jobs",
            data=json.dumps({
                "project": "demo", "host": "idlehost",
                "client_version": "0.2.0", "slot": "light",
                "cmd": ["true"], "sha": ""}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as resp:
            self.assertEqual(resp.status, 201)
            job = json.loads(resp.read())
        self.assertEqual(job["status"], "queued")
        # 新增字段以默认值出现，旧字段不变
        self.assertEqual(job["webhooks"], [])
        self.assertEqual(job["hold_seconds"], 0)
        self.assertIn("queue", job)
        self._cancel(job["id"])

        # 不带 wait 的 GET 不返回长轮询头（0.2 行为）
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/jobs/{job['id']}",
                headers={"Authorization": f"Bearer {TOKEN}"}), timeout=10) as resp:
            self.assertIsNone(resp.headers.get("X-Calltable-Wait-Result"))
            self.assertEqual(json.loads(resp.read())["status"], "cancelled")

    # ------------------------------------------------------------ 回执 + 等待 + webhook
    def test_receipt_wait_and_signed_webhooks(self):
        receiver = Receiver()
        try:
            result = self._submit("--notify", receiver.url, "--detach",
                                  label="receipt")
            self.assertEqual(result.returncode, 0, result.stderr)
            job_id = job_id_from(result.stdout)
            self.assertIn(f"queued {job_id}", result.stdout)
            self.assertIn("queue:", result.stdout)
            self.assertIn("estimate:", result.stdout)
            self.assertIn("notify: 1 webhook(s)", result.stdout)

            job = self._api(f"/api/jobs/{job_id}")
            self.assertEqual(job["webhooks"], [receiver.url])
            self.assertIn("queue", job)
            self.assertIn("runtime_seconds", job["queue"])

            waited = self._cli("wait", job_id, "--until", "done",
                               "--timeout", "120")
            self.assertEqual(waited.returncode, 0, waited.stderr)
            self.assertIn("[succeeded] exit=0", waited.stdout)

            self._wait_until(lambda: len(receiver.requests) >= 2, timeout=30,
                             message="two webhook deliveries")
            events = receiver.events()
            self.assertEqual([event["type"] for event in events],
                             ["job.started", "job.finished"])
            from calltable.webhooks import verify
            for item in list(receiver.requests):
                headers = item["headers"]
                self.assertTrue(headers["webhook-id"].startswith("msg_"))
                self.assertTrue(headers["webhook-signature"].startswith("v1,"))
                self.assertTrue(verify(WEBHOOK_SECRET, headers["webhook-id"],
                                       headers["webhook-timestamp"],
                                       item["body"],
                                       headers["webhook-signature"],
                                       tolerance=300))
            finished = events[1]
            self.assertEqual(finished["job"]["id"], job_id)
            self.assertEqual(finished["job"]["status"], "succeeded")
            self.assertEqual(finished["job"]["exit_code"], 0)
            self.assertIn("/#/job/", finished["dashboard_url"])

            self._wait_until(
                lambda: all(
                    event["status"] == "delivered"
                    for entry in self._api(f"/api/jobs/{job_id}")[
                        "webhook_deliveries"]
                    for event in entry["events"]),
                timeout=20, message="deliveries to be marked delivered")
            shown = self._cli("show", job_id)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("webhooks", shown.stdout)
            self.assertIn("delivered", shown.stdout)
        finally:
            receiver.close()

    def test_webhook_failures_retry_with_the_same_message_id(self):
        receiver = Receiver(fail_started=2)
        try:
            result = self._submit("--notify", receiver.url, "--detach",
                                  label="retry")
            self.assertEqual(result.returncode, 0, result.stderr)
            job_id = job_id_from(result.stdout)
            self._wait_status(job_id, ("succeeded", "failed"), timeout=60)
            self._wait_until(lambda: len(receiver.requests) >= 4, timeout=30,
                             message="retries + finished delivery")
            requests = list(receiver.requests)
            by_type: dict[str, list[dict]] = {}
            for item in requests:
                by_type.setdefault(json.loads(item["body"])["type"],
                                   []).append(item)
            # started 前两次 500，第三次成功；finished 一次成功。
            self.assertGreaterEqual(len(by_type["job.started"]), 3)
            self.assertGreaterEqual(len(by_type["job.finished"]), 1)
            for event_type, items in by_type.items():
                ids = {item["headers"]["webhook-id"] for item in items}
                self.assertEqual(len(ids), 1,
                                 f"webhook-id must be stable across retries "
                                 f"({event_type})")
                timestamps = [int(item["headers"]["webhook-timestamp"])
                              for item in items]
                self.assertEqual(timestamps, sorted(timestamps))

            def delivered():
                detail = self._api(f"/api/jobs/{job_id}")
                return all(event["status"] == "delivered"
                           for entry in detail["webhook_deliveries"]
                           for event in entry["events"])

            self._wait_until(delivered, timeout=20,
                             message="retried deliveries to succeed")
        finally:
            receiver.close()

    # ------------------------------------------------------------ 预计时间
    def test_eta_is_reported_once_history_exists(self):
        for index in range(3):
            result = self._submit("--wait", label=f"history-{index}")
            self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self._submit("--detach", label="eta-probe")
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        job_id = job_id_from(receipt.stdout)
        job = self._api(f"/api/jobs/{job_id}")
        self.assertIsNotNone(job["queue"]["runtime_seconds"],
                             f"queue={job['queue']}")
        self.assertGreaterEqual(job["queue"]["runtime_samples"], 3)
        self.assertIn("median of", receipt.stdout)
        self._wait_status(job_id, ("succeeded", "failed", "error"), timeout=60)

    # ------------------------------------------------------------ 幂等
    def test_idempotent_key_reuses_one_ticket(self):
        first = self._submit("--detach", "--idempotency-key", "e2e-idem-1",
                             label="idem")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_id = job_id_from(first.stdout)
        second = self._submit("--detach", "--idempotency-key", "e2e-idem-1",
                              label="idem")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already queued", second.stdout)
        self.assertEqual(job_id_from(second.stdout), first_id)
        self.assertIn("idempotency: replayed", second.stdout)
        self._wait_status(first_id, ("succeeded", "failed", "error"), timeout=60)

        jobs = self._api("/api/jobs?limit=200")["jobs"]
        self.assertEqual(sum(1 for job in jobs if job["label"] == "idem"), 1)

        # 脏工作区：每次 ct run 都会生成不同的快照 sha，重试仍必须复用。
        dirty_file = self.repo / "dirty-for-idem.txt"
        dirty_file.write_text("dirty", encoding="utf-8")
        try:
            dirty_first = self._submit("--detach", "--idempotency-key",
                                       "e2e-idem-dirty", label="idem-dirty")
            self.assertEqual(dirty_first.returncode, 0, dirty_first.stderr)
            dirty_second = self._submit("--detach", "--idempotency-key",
                                        "e2e-idem-dirty", label="idem-dirty")
            self.assertEqual(dirty_second.returncode, 0, dirty_second.stderr)
            self.assertEqual(job_id_from(dirty_first.stdout),
                             job_id_from(dirty_second.stdout))
        finally:
            dirty_file.unlink(missing_ok=True)

        conflict = self._submit("--detach", "--idempotency-key", "e2e-idem-1",
                                label="idem-other")
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("different", conflict.stderr)

    # ------------------------------------------------------------ 排队心跳
    def test_queue_hold_expires_without_heartbeat(self):
        blockers = []
        try:
            for index in range(2):
                result = self._submit(
                    "--detach", label=f"hold-blocker-{index}",
                    command=["bash", "-c", "sleep 90"])
                self.assertEqual(result.returncode, 0, result.stderr)
                blockers.append(job_id_from(result.stdout))
            for job_id in blockers:
                self._wait_status(job_id, ("running",), timeout=60)

            held = self._submit("--detach", "--hold-while-alive",
                                "--hold-timeout", "2s", label="hold-me")
            self.assertEqual(held.returncode, 0, held.stderr)
            held_id = job_id_from(held.stdout)
            self.assertIn("hold:", held.stdout)
            job = self._api(f"/api/jobs/{held_id}")
            self.assertEqual(job["hold_seconds"], 2)
            self.assertEqual(job["status"], "queued")

            expired = self._wait_status(held_id, ("cancelled",), timeout=30)
            self.assertEqual(expired["exit_code"], -2)
            self.assertIn("queue hold expired", expired["error"])
            shown = self._cli("show", held_id)
            self.assertIn("hold", shown.stdout)
        finally:
            self._drain_running()

    def test_ct_wait_heartbeats_a_held_ticket(self):
        blockers = []
        waiter = None
        try:
            for index in range(2):
                result = self._submit(
                    "--detach", label=f"hb-blocker-{index}",
                    command=["bash", "-c", "sleep 90"])
                self.assertEqual(result.returncode, 0, result.stderr)
                blockers.append(job_id_from(result.stdout))
            for job_id in blockers:
                self._wait_status(job_id, ("running",), timeout=60)

            held = self._submit("--detach", "--hold-while-alive",
                                "--hold-timeout", "6s", label="hb-hold")
            self.assertEqual(held.returncode, 0, held.stderr)
            held_id = job_id_from(held.stdout)

            waiter = subprocess.Popen(
                [sys.executable, "-m", "calltable", "cli", "wait",
                 "--config", str(self.config_path), held_id,
                 "--until", "done", "--timeout", "20s"],
                cwd=str(REPO_ROOT), env=self.env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            # 心跳间隔被压缩到 hold/3（<=2s），6s 的 hold 不应过期。
            time.sleep(8)
            self.assertEqual(self._api(f"/api/jobs/{held_id}")["status"],
                             "queued")
            # 心跳保住了号码；不用等它的 --timeout（退出码 75 由别的用例覆盖）。
            waiter.terminate()
            waiter.communicate(timeout=30)
        finally:
            if waiter is not None and waiter.poll() is None:
                waiter.terminate()
            self._drain_running()

    # ------------------------------------------------------------ 等待退出码
    def test_wait_until_started_and_local_timeout(self):
        try:
            result = self._submit("--detach", label="started-probe",
                                  command=["bash", "-c", "sleep 8"])
            self.assertEqual(result.returncode, 0, result.stderr)
            job_id = job_id_from(result.stdout)

            started = self._cli("wait", job_id, "--until", "started",
                                "--timeout", "60")
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertIn("[started]", started.stdout)
            self.assertEqual(self._api(f"/api/jobs/{job_id}")["status"],
                             "running")

            timed_out = self._cli("wait", job_id, "--until", "done",
                                  "--timeout", "1s")
            self.assertEqual(timed_out.returncode, 75, timed_out.stderr)
            self.assertIn("local wait timed out", timed_out.stderr)
        finally:
            self._drain_running()


if __name__ == "__main__":
    unittest.main()
