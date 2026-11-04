"""F1/B1 真机回归：执行器重启、hub 重启、finish 重试（子进程级）。

用临时目录 + 127.0.0.1 随机端口，不触碰真实仓库/数据。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "recovery-token"


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
    raise AssertionError("no queued job in output: " + stdout)


def _pid_alive(pid: int) -> bool:
    if __import__("os").name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        __import__("os").kill(pid, 0)
        return True
    except OSError:
        return False


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.port = free_port()
        cls.repo = cls.root / "demo"
        cls.repo.mkdir()
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "config", "user.email", "r@e")
        git(cls.repo, "config", "user.name", "r")
        (cls.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-qm", "init")

        profiles = cls.root / "profiles"
        profiles.mkdir()
        (profiles / "demo@testhost.json").write_text(json.dumps({
            "name": "demo@testhost", "slot": "light", "timeout": "5m",
            "artifacts": []}), encoding="utf-8")

        cls.config_path = cls.root / "config.json"
        cls.config_path.write_text(json.dumps({
            "token": TOKEN,
            "data_dir": str(cls.root / "data"),
            "profiles_dir": str(profiles),
            "hub_url": f"http://127.0.0.1:{cls.port}",
            "hub_bind": "127.0.0.1",
            "hub_port": cls.port,
            "hosts": {"testhost": {"slots": {"heavy": 1, "light": 2}}},
            "exec_host": "testhost",
            "runs_root": str(cls.root / "runs"),
            "mirror_root": str(cls.root / "mirrors"),
            "max_jobs": 2,
            "poll_interval": 0.3,
            "sample_interval": 0.3,
            "use_systemd": False,
            "retention_success_seconds": 3600,
            "retention_failure_seconds": 3600,
        }, indent=2), encoding="utf-8")

        cls.env = os.environ.copy()
        cls.env["PYTHONPATH"] = str(REPO_ROOT)
        cls.env["PYTHONUNBUFFERED"] = "1"
        cls.logs = (cls.root / "logs")
        cls.logs.mkdir()
        cls.hub = None
        cls.executor = None
        cls._start_hub()

    @classmethod
    def tearDownClass(cls):
        cls._stop_executor()
        cls._stop_hub()
        cls.tmp.cleanup()

    # ------------------------------------------------------------ 进程
    @classmethod
    def _start_hub(cls):
        handle = (cls.logs / f"hub-{time.time_ns()}.log").open("w", encoding="utf-8")
        cls.hub_log = handle
        cls.hub = subprocess.Popen(
            [sys.executable, "-m", "calltable", "hub", "--config",
             str(cls.config_path)],
            cwd=str(REPO_ROOT), env=cls.env, stdout=handle,
            stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{cls.port}/api/health", timeout=2):
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        raise RuntimeError("hub did not start")

    @classmethod
    def _stop_hub(cls):
        if cls.hub and cls.hub.poll() is None:
            cls.hub.terminate()
            try:
                cls.hub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.hub.kill()
        cls.hub = None
        handle = getattr(cls, "hub_log", None)
        if handle and not handle.closed:
            handle.close()

    @classmethod
    def _start_executor(cls):
        handle = (cls.logs / f"exec-{time.time_ns()}.log").open("w", encoding="utf-8")
        cls.exec_log = handle
        cls.executor = subprocess.Popen(
            [sys.executable, "-m", "calltable", "exec", "--config",
             str(cls.config_path)],
            cwd=str(REPO_ROOT), env=cls.env, stdout=handle,
            stderr=subprocess.STDOUT)
        cls._wait_worker()

    @classmethod
    def _stop_executor(cls):
        if cls.executor and cls.executor.poll() is None:
            cls.executor.terminate()
            try:
                cls.executor.wait(timeout=15)
            except subprocess.TimeoutExpired:
                cls.executor.kill()
        cls.executor = None
        handle = getattr(cls, "exec_log", None)
        if handle and not handle.closed:
            handle.close()

    # ------------------------------------------------------------ API
    @classmethod
    def _api(cls, path, method="GET"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}", method=method,
            headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None

    @classmethod
    def _host(cls):
        return next(h for h in cls._api("/api/state")["hosts"]
                    if h["name"] == "testhost")

    @classmethod
    def _wait_worker(cls, previous: str | None = None, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            host = cls._host()
            worker = host.get("worker") or ""
            if worker and worker != previous:
                return worker
            time.sleep(0.1)
        raise AssertionError("executor never heartbeat")

    @classmethod
    def _wait_job(cls, job_id, predicate, timeout=60):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            job = cls._api(f"/api/jobs/{job_id}")
            if predicate(job):
                return job
            time.sleep(0.2)
        raise AssertionError(f"job {job_id} did not reach the expected state: {job}")

    @classmethod
    def _wait_log(cls, job_id: str, text: str, timeout: float = 60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            req = urllib.request.Request(
                f"http://127.0.0.1:{cls.port}/api/jobs/{job_id}/logs?offset=0",
                headers={"Authorization": f"Bearer {TOKEN}"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if text.encode() in resp.read():
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.2)
        raise AssertionError(f"log never contained {text!r}")

    def _submit(self, label: str, command: list[str]) -> str:
        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path), "--host", "testhost",
             "--project", "demo", "--repo", str(self.repo),
             "--label", label, "--detach", "--", *command],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        return job_id_from(cli.stdout)

    # ------------------------------------------------------------ 用例
    def test_executor_restart_reaps_orphaned_job(self):
        """执行器被 kill 后再起一个新实例，旧任务必须被回收，槽位释放。"""
        self._start_executor()
        first_worker = self._host()["worker"]
        job_id = self._submit("orphan", ["bash", "-c", "echo started; sleep 120"])
        self._wait_job(job_id, lambda j: j["status"] == "running")

        # 模拟执行器崩溃
        self._stop_executor()
        self.assertEqual(self._host()["used"].get("light", 0), 1)

        self._start_executor()
        self._wait_worker(previous=first_worker)
        job = self._wait_job(
            job_id, lambda j: j["status"] in ("error", "failed", "cancelled"))
        self.assertEqual(job["status"], "error")
        # 新执行器启动时会主动回收旧进程；若来不及，hub 的 worker 变更
        # 对账也会给出同义结论。
        self.assertTrue("worker changed" in job["error"]
                        or "orphan job terminated" in job["error"],
                        job["error"])
        host = self._host()
        self.assertEqual(host["used"].get("light", 0), 0)
        self._stop_executor()

    def test_main_exit_wins_over_grandchild_pipe(self):
        """主进程 exit 3 后，继承 stdout 的 sleep 不能把任务拖住。"""
        self._start_executor()
        job_id = self._submit(
            "grandchild",
            ["bash", "-c", "sleep 600 & echo $! > grandchild.pid; exit 3"])
        started = time.time()
        job = self._wait_job(
            job_id, lambda j: j["status"] in ("failed", "error", "succeeded"),
            timeout=30)
        elapsed = time.time() - started
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(job["exit_code"], 3)
        runtime = job["finished"] - job["started"]
        self.assertLess(runtime, 15.0, f"job runtime {runtime:.1f}s")
        self.assertLess(elapsed, 30.0, f"observed {elapsed:.1f}s")

        pidfile = self.root / "runs" / job_id / "work" / "grandchild.pid"
        pid = int(pidfile.read_text().strip())
        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)
        self.assertFalse(_pid_alive(pid), f"grandchild {pid} survived")
        self._stop_executor()

    def test_hub_restart_keeps_live_job_and_finish_retries(self):
        """hub 重启：正在跑的任务靠心跳对账存活；期间的 finish 会重试送达。"""
        self._start_executor()
        job_id = self._submit("survivor", ["bash", "-c", "echo alive; sleep 8"])
        self._wait_job(job_id, lambda j: j["status"] == "running")
        self._wait_log(job_id, "alive")  # 确认已经进入命令执行阶段

        self._stop_hub()
        time.sleep(2)  # 覆盖 finish 上报失败 + 重试窗口
        self._start_hub()
        self._wait_worker()  # 新 hub 收到同一 worker 的心跳

        job = self._wait_job(job_id, lambda j: j["status"] in
                             ("succeeded", "failed", "error", "cancelled"),
                             timeout=90)
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertEqual(job["exit_code"], 0)
        host = self._host()
        self.assertEqual(host["used"].get("light", 0), 0)
        self._stop_executor()


if __name__ == "__main__":
    unittest.main()
