"""端到端：本机起 hub + executor，提交任务、跑命令、收产物。

用临时 git 仓库和 127.0.0.1 上的随机端口，不触碰任何真实仓库或数据。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "e2e-token"

COMMAND = (
    "print(1)\n"
    "print('hello from job')\n"
    "import os\n"
    "os.makedirs('artifacts', exist_ok=True)\n"
    "open('artifacts/out.json', 'w', encoding='utf-8').write('{\"ok\": true}')\n"
    "open('artifacts/big.bin', 'wb').write(b'x' * (2 * 1024 * 1024))\n"
)
EXPECTED_ARTIFACT = '{"ok": true}'
BIG_ARTIFACT_SIZE = 2 * 1024 * 1024


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


class EndToEndTests(unittest.TestCase):
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
            "name": "demo@testhost",
            "slot": "light",
            "artifacts": ["artifacts/*"],
            "timeout": "2m",
        }), encoding="utf-8")
        (profiles / "ruled@testhost.json").write_text(json.dumps({
            "name": "ruled@testhost",
            "slot": "light",
            "locks": [],
            "timeout": "1h",
            "rules": [{
                "match": r"gate\.sh", "slot": "heavy",
                "locks": ["ruled-gate@{host}"], "timeout": "4h",
            }],
        }), encoding="utf-8")

        cls.config_path = root / "config.json"
        cls.config_path.write_text(json.dumps({
            "token": TOKEN,
            "data_dir": str(root / "data"),
            "profiles_dir": str(profiles),
            "hub_url": f"http://127.0.0.1:{cls.port}",
            "hub_bind": "127.0.0.1",
            "hub_port": cls.port,
            "hosts": {"testhost": {"slots": {"heavy": 1, "light": 2}}},
            "exec_host": "testhost",
            "runs_root": str(root / "runs"),
            "mirror_root": str(root / "mirrors"),
            "max_jobs": 2,
            "poll_interval": 0.5,
            "sample_interval": 1,
            "use_systemd": False,
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
        for handle in (getattr(cls, "exec_log", None), getattr(cls, "hub_log", None)):
            if handle:
                handle.close()
        cls.tmp.cleanup()

    # ------------------------------------------------------------ helpers
    @classmethod
    def _api(cls, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
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
                    raise RuntimeError("hub exited early:\n" +
                                       (cls.root / "hub.log").read_text(
                                           encoding="utf-8", errors="replace"))
                time.sleep(0.2)
        raise RuntimeError("hub did not become healthy")

    @classmethod
    def _wait_host_online(cls, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = cls._api("/api/state")
            for host in state["hosts"]:
                if host["name"] == "testhost" and host["online"]:
                    return
            time.sleep(0.3)
        raise RuntimeError("executor did not heartbeat:\n" +
                           (cls.root / "exec.log").read_text(
                               encoding="utf-8", errors="replace"))

    # ------------------------------------------------------------ 用例
    # ------------------------------------------------------------ health/版本
    def test_health_reports_server_version_for_doctor(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/health", timeout=10) as resp:
            payload = json.loads(resp.read())
        from calltable import RULES_CLIENT_VERSION, __version__
        self.assertEqual(payload["version"], __version__)
        self.assertEqual(payload["rules_client_version"], RULES_CLIENT_VERSION)
        self.assertGreaterEqual(payload["version"], "0.2.0")

    # ------------------------------------------------------------ 旧客户端
    def test_legacy_client_is_recomputed_from_rules(self):
        # 不带 client_version（等价于 0.1.0）：slot/locks/timeout 全部忽略
        legacy = self._api("/api/jobs", method="POST", body={
            "project": "ruled", "host": "testhost", "slot": "light",
            "locks": [], "timeout": 3600, "cmd": ["bash", "gate.sh"],
            "sha": ""})
        self.assertEqual(legacy["slot"], "heavy")
        self.assertEqual(legacy["locks"], ["ruled-gate@{host}"])
        self.assertEqual(legacy["timeout"], 4 * 3600)
        job = self._api(f"/api/jobs/{legacy['id']}")
        self.assertEqual(job["status"], "queued")
        self._api(f"/api/jobs/{legacy['id']}/cancel", method="POST", body={})

        # 新客户端显式值优先
        from calltable import __version__
        modern = self._api("/api/jobs", method="POST", body={
            "project": "ruled", "host": "testhost", "slot": "light",
            "client_version": __version__, "cmd": ["bash", "gate.sh"],
            "sha": ""})
        self.assertEqual(modern["slot"], "light")
        self.assertEqual(modern["locks"], ["ruled-gate@{host}"])
        self._api(f"/api/jobs/{modern['id']}/cancel", method="POST", body={})

    # ------------------------------------------------------------ drain
    def test_drain_stops_dispatch_and_undrain_resumes(self):
        host = self._api("/api/hosts/testhost/drain", method="POST",
                         body={"drain": True})
        self.assertTrue(host["draining"])
        try:
            cli = subprocess.run(
                [sys.executable, "-m", "calltable", "cli", "run",
                 "--config", str(self.config_path), "--host", "testhost",
                 "--project", "demo", "--repo", str(self.repo),
                 "--label", "drain-probe",
                 "--", sys.executable.replace("\\", "/"), "-c", "print(2)"],
                cwd=str(REPO_ROOT), env=self.env, capture_output=True,
                text=True, timeout=120)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            job_id = job_id_from(cli.stdout)
            time.sleep(3)
            self.assertEqual(self._api(f"/api/jobs/{job_id}")["status"], "queued")
            self._api("/api/hosts/testhost/drain", method="POST",
                      body={"drain": False})
            deadline = time.time() + 60
            while time.time() < deadline:
                if self._api(f"/api/jobs/{job_id}")["status"] in (
                        "succeeded", "failed", "error"):
                    break
                time.sleep(0.5)
            self.assertEqual(self._api(f"/api/jobs/{job_id}")["status"],
                             "succeeded")
        finally:
            self._api("/api/hosts/testhost/drain", method="POST",
                      body={"drain": False})

    # ------------------------------------------------------------ 断点续传
    def _submit_detached(self, label):
        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path), "--host", "testhost",
             "--project", "demo", "--repo", str(self.repo), "--label", label,
             "--", sys.executable.replace("\\", "/"), "-c", "print(3)"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=180)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        return job_id_from(cli.stdout)

    def test_bundle_download_supports_range_and_resume(self):
        from calltable.client import HubClient
        # 新提交一个 commit，保证这次一定会上传 bundle（否则 hub 已有时是 204）
        (self.repo / "range-probe.txt").write_text(
            str(time.time()), encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "range probe")
        job_id = self._submit_detached("range-probe")
        client = HubClient(f"http://127.0.0.1:{self.port}", TOKEN)
        full = self.root / "full.bundle"
        client.download(f"/api/jobs/{job_id}/bundle", full, attempts=1)
        data = full.read_bytes()
        self.assertGreater(len(data), 32)

        # 服务端支持 Range：返回 206 + Content-Range，body 是剩余部分
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/jobs/{job_id}/bundle",
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Range": "bytes=16-"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.headers.get("Content-Range"),
                             f"bytes 16-{len(data) - 1}/{len(data)}")
            self.assertEqual(resp.read(), data[16:])

        # 客户端把已下载的前半截续上，结果必须与整包一致
        partial = self.root / "resume.bundle"
        partial.write_bytes(data[: max(1, len(data) // 2)])
        client.download(f"/api/jobs/{job_id}/bundle", partial, attempts=1)
        self.assertEqual(partial.read_bytes(), data)

        # 文件更长（本地残留）时服务端 416，客户端应重下
        stale = self.root / "stale.bundle"
        stale.write_bytes(b"x" * (len(data) + 100))
        client.download(f"/api/jobs/{job_id}/bundle", stale, attempts=2)
        self.assertEqual(stale.read_bytes(), data)

        # 等这个探针任务结束，避免影响后面的槽位断言
        deadline = time.time() + 120
        while time.time() < deadline:
            if self._api(f"/api/jobs/{job_id}")["status"] in (
                    "succeeded", "failed", "error"):
                break
            time.sleep(0.5)

    def test_submit_run_logs_and_artifact(self):
        # 未跟踪文件也要进快照（这里放一个不会被任务改动的文件）
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        before = git(self.repo, "status", "--porcelain")

        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path),
             "--host", "testhost", "--project", "demo",
             "--repo", str(self.repo), "--wait", "--label", "e2e",
             "--agent", "e2e-agent",
             "--", sys.executable.replace("\\", "/"), "-c", COMMAND],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=180)
        self.assertEqual(cli.returncode, 0,
                         f"stdout:\n{cli.stdout}\nstderr:\n{cli.stderr}")
        self.assertIn("1", cli.stdout)
        self.assertIn("hello from job", cli.stdout)
        self.assertIn("[succeeded] exit=0", cli.stdout)

        # 工作区没有被修改
        self.assertEqual(git(self.repo, "status", "--porcelain"), before)

        jobs = self._api("/api/jobs")["jobs"]
        job = next(j for j in jobs if j["label"] == "e2e")
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["exit_code"], 0)
        self.assertTrue(job["dirty"])
        self.assertEqual(len(job["sha"]), 40)
        self.assertEqual(job["agent"], "e2e-agent")
        self.assertRegex(job["submitter"], r"^[^/]+/[^/]+/[^@]+@.+$")

        logs, _ = self._read_logs(job["id"])
        self.assertIn("hello from job", logs)

        detail = self._api(f"/api/jobs/{job['id']}")
        names = {a["name"]: a for a in detail["artifacts"]}
        self.assertEqual(detail.get("error"), "",
                         f"artifact collection error: {detail.get('error')}")
        self.assertIn("artifacts/out.json", names)
        artifact = names["artifacts/out.json"]
        self.assertEqual(artifact["sha256"],
                         hashlib.sha256(EXPECTED_ARTIFACT.encode()).hexdigest())

        self.assertIn("artifacts/big.bin", names, f"got artifacts: {sorted(names)}")
        big = names["artifacts/big.bin"]
        self.assertEqual(big["size"], BIG_ARTIFACT_SIZE)
        self.assertEqual(big["sha256"],
                         hashlib.sha256(b"x" * BIG_ARTIFACT_SIZE).hexdigest())

        samples = self._api(f"/api/jobs/{job['id']}/samples")["samples"]
        # 任务很短，采样可能只有 0 或 1 条；只要求接口可用
        self.assertIsInstance(samples, list)

        # 主机槽位在任务结束后应当释放
        state = self._api("/api/state")
        host = next(h for h in state["hosts"] if h["name"] == "testhost")
        self.assertEqual(host["used"].get("light", 0), 0)
        self.assertEqual(host["locks"], [])

    def test_dashboard_renders_with_token(self):
        html = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/?token={TOKEN}", timeout=10).read().decode()
        self.assertIn("calltable", html)
        self.assertIn(TOKEN, html)
        self.assertNotIn("__TOKEN__", html)
        self.assertIn("/api/state", html)

    def test_wait_reattaches_and_returns_job_exit_code(self):
        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path),
             "--host", "testhost", "--project", "demo",
             "--repo", str(self.repo), "--label", "wait-me", "--detach",
             "--", "bash", "-c", "echo waited; sleep 1; exit 5"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        job_id = job_id_from(cli.stdout)

        waited = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "wait",
             "--config", str(self.config_path), job_id],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=180)
        self.assertEqual(waited.returncode, 5, waited.stderr)
        self.assertIn("waited", waited.stdout)

    def test_cancel_running_job_releases_slot(self):
        (self.repo / "keep.txt").write_text("x\n", encoding="utf-8")
        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path),
             "--host", "testhost", "--project", "demo",
             "--repo", str(self.repo), "--label", "cancel-me", "--detach",
             "--", "bash", "-c", "echo started; sleep 120"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        job_id = job_id_from(cli.stdout)

        deadline = time.time() + 60
        while time.time() < deadline:
            job = self._api(f"/api/jobs/{job_id}")
            if job["status"] == "running":
                break
            time.sleep(0.2)
        else:
            self.fail("job never started")

        self._api(f"/api/jobs/{job_id}/cancel", method="POST")
        deadline = time.time() + 60
        while time.time() < deadline:
            job = self._api(f"/api/jobs/{job_id}")
            if job["status"] in ("cancelled", "failed", "succeeded", "error"):
                break
            time.sleep(0.3)
        self.assertEqual(job["status"], "cancelled")
        state = self._api("/api/state")
        host = next(h for h in state["hosts"] if h["name"] == "testhost")
        self.assertEqual(host["used"].get("light", 0), 0)

    def test_auth_is_header_only_except_artifact_download(self):
        # 1) query token 不能用于普通 API
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/state?token={TOKEN}", timeout=10)
        self.assertEqual(ctx.exception.code, 401)

        # 2) 看板页面本身公开（token 只在注入时生效）
        html = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/?token={TOKEN}", timeout=10).read().decode()
        self.assertIn(TOKEN, html)

        # 3) 产物下载允许 query token，但也接受 header
        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path),
             "--host", "testhost", "--project", "demo",
             "--repo", str(self.repo), "--wait", "--label", "auth-check",
             "--", sys.executable.replace("\\", "/"), "-c",
             "import os;os.makedirs('artifacts',exist_ok=True);"
             "open('artifacts/auth.txt','w').write('ok')"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=180)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        jobs = self._api("/api/jobs")["jobs"]
        job = next(j for j in jobs if j["label"] == "auth-check")

        url = (f"http://127.0.0.1:{self.port}/api/jobs/{job['id']}"
               f"/artifacts/artifacts%2Fauth.txt")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=10)
        self.assertEqual(ctx.exception.code, 401)

        with urllib.request.urlopen(url + f"?token={TOKEN}", timeout=10) as resp:
            self.assertEqual(resp.read(), b"ok")

        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.read(), b"ok")

    def test_concurrent_jobs_keep_their_own_origin_main(self):
        head = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        base_one = git(self.repo, "commit-tree", tree, "-p", head, "-m", "base-one")
        base_two = git(self.repo, "commit-tree", tree, "-p", head, "-m", "base-two")
        command = ["bash", "-c",
                   "git rev-parse origin/main; git merge-base HEAD origin/main; sleep 2"]

        def submit(base: str, label: str) -> str:
            git(self.repo, "update-ref", "refs/remotes/origin/main", base)
            cli = subprocess.run(
                [sys.executable, "-m", "calltable", "cli", "run",
                 "--config", str(self.config_path),
                 "--host", "testhost", "--project", "demo",
                 "--repo", str(self.repo), "--detach", "--label", label,
                 "--base-ref", "origin/main", "--", *command],
                cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
                timeout=120)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            return job_id_from(cli.stdout)

        try:
            # 两次提交之间把本地 origin/main 指到不同的提交；两个任务会并发跑。
            first = submit(base_one, "base-one")
            second = submit(base_two, "base-two")
            for job_id in (first, second):
                deadline = time.time() + 120
                while time.time() < deadline:
                    job = self._api(f"/api/jobs/{job_id}")
                    if job["status"] in ("succeeded", "failed", "error",
                                         "cancelled", "timeout"):
                        break
                    time.sleep(0.2)
                self.assertEqual(job["status"], "succeeded",
                                 f"{job_id}: {job.get('error')}")

            job_one = self._api(f"/api/jobs/{first}")
            job_two = self._api(f"/api/jobs/{second}")
            self.assertEqual(job_one["base_ref_sha"], base_one)
            self.assertEqual(job_two["base_ref_sha"], base_two)
            self.assertEqual(job_one["base_ref"], "origin/main")

            log_one, _ = self._read_logs(first)
            log_two, _ = self._read_logs(second)
            self.assertIn(base_one, log_one)
            self.assertNotIn(base_two, log_one)
            self.assertIn(base_two, log_two)
            self.assertNotIn(base_one, log_two)

            # merge-base 必须和本地对同一快照提交算出来的一致
            expected_one = git(self.repo, "merge-base", job_one["sha"], base_one)
            expected_two = git(self.repo, "merge-base", job_two["sha"], base_two)
            self.assertIn(expected_one, log_one)
            self.assertIn(expected_two, log_two)
        finally:
            git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")

    def test_project_names_are_submitted_verbatim(self):
        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path),
             "--host", "testhost", "--project", "verbatim-proj",
             "--repo", str(self.repo), "--wait", "--label", "verbatim",
             "--", "bash", "-c", "echo verbatim-ok"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=180)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertNotIn("note:", cli.stderr)
        jobs = self._api("/api/jobs")["jobs"]
        job = next(j for j in jobs if j["label"] == "verbatim")
        self.assertEqual(job["project"], "verbatim-proj")

    def test_clean_tree_uses_real_head_and_exports_baseline(self):
        clean = self.root / "clean-repo"
        clean.mkdir()
        git(clean, "init", "-q", "-b", "main")
        git(clean, "config", "user.email", "clean@example.com")
        git(clean, "config", "user.name", "clean")
        (clean / "a.txt").write_text("a\n", encoding="utf-8")
        git(clean, "add", "-A")
        git(clean, "commit", "-qm", "init")
        head = git(clean, "rev-parse", "HEAD")
        git(clean, "update-ref", "refs/remotes/origin/main", head)

        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path),
             "--host", "testhost", "--project", "demo",
             "--repo", str(clean), "--wait", "--label", "clean-tree",
             "--", "bash", "-c",
             'echo "CALLTABLE_SHA=$CALLTABLE_SHA BASE=$CALLTABLE_BASE_SHA DIRTY=$CALLTABLE_DIRTY '
             'REF=$CALLTABLE_BASE_REF_SHA"'],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=180)
        self.assertEqual(cli.returncode, 0, cli.stderr)

        jobs = self._api("/api/jobs")["jobs"]
        job = next(j for j in jobs if j["label"] == "clean-tree")
        self.assertFalse(job["dirty"])
        self.assertEqual(job["sha"], head)
        self.assertEqual(job["base_sha"], head)
        self.assertEqual(job["base_ref_sha"], head)
        self.assertEqual(job["base_ref"], "origin/main")

        logs, _ = self._read_logs(job["id"])
        self.assertIn(f"CALLTABLE_SHA={head}", logs)
        self.assertIn(f"BASE={head}", logs)
        self.assertIn("DIRTY=0", logs)
        self.assertIn(f"REF={head}", logs)

        shown = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "show",
             "--config", str(self.config_path), job["id"]],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=60)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("base      origin/main", shown.stdout)
        self.assertNotIn("(dirty)", shown.stdout)

        # 看板模板确实渲染 base 与 dirty
        from calltable.dashboard import PAGE
        self.assertIn("job.base_ref", PAGE)
        self.assertIn("job.dirty", PAGE)

    def _read_logs(self, job_id):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/jobs/{job_id}/logs?offset=0",
            headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace"), resp.headers


if __name__ == "__main__":
    unittest.main()
