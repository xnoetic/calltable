"""Cloud Run provider 端到端：真实 hub 子进程 + 假 gcloud。

假 gcloud 收到 ``run jobs execute`` 时，用 Cloud Run 镜像里同一个
``entrypoint.sh`` 在本机起一次性执行器（不带 TS_AUTHKEY，所以不启动
tailscale），以此验证：预留 → execute（环境变量传 job id）→ attach →
跑命令/传日志 → 终态 → execution 对账收尾。不调用真实 gcloud，不产生费用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_e2e import free_port, git, job_id_from, tail

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "cloudrun-e2e-token"
ENTRYPOINT = REPO_ROOT / "calltable" / "cloudrun_assets" / "entrypoint.sh"

FAKE_GCLOUD = r'''#!PYTHON
"""Fake gcloud for calltable tests: emulates `run jobs execute/executions`."""
import json, os, signal, subprocess, sys
from pathlib import Path

state = Path(os.environ["FAKE_GCLOUD_STATE"])
state.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
with (state / "calls.log").open("a") as log:
    log.write(json.dumps(args) + "\n")

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # reap zombies of our own children is not possible here; check /proc
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True

if args[:3] == ["run", "jobs", "execute"]:
    job = args[3]
    env = dict(os.environ)
    for arg in args:
        if arg.startswith("--update-env-vars="):
            for pair in arg.split("=", 1)[1].split(","):
                key, _, value = pair.partition("=")
                env[key] = value
    count = len(list(state.glob("*.pid"))) + 1
    name = f"{job}-fake{count}"
    log = open(state / f"{name}.log", "w")
    proc = subprocess.Popen(["sh", os.environ["FAKE_ENTRYPOINT"]], env=env,
                            stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    (state / f"{name}.pid").write_text(str(proc.pid))
    print(json.dumps({"metadata": {"name": name}}))
    sys.exit(0)
if args[:4] == ["run", "jobs", "executions", "describe"]:
    name = args[4]
    pid = int((state / f"{name}.pid").read_text())
    if alive(pid):
        print(json.dumps({"status": {}}))
    else:
        print(json.dumps({"status": {"completionTime": "2026-11-25T00:00:00Z",
                                     "succeededCount": 1}}))
    sys.exit(0)
if args[:4] == ["run", "jobs", "executions", "cancel"]:
    name = args[4]
    try:
        os.killpg(int((state / f"{name}.pid").read_text()), signal.SIGKILL)
    except OSError:
        pass
    sys.exit(0)
print("unsupported fake gcloud call: " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"),
                 "the Cloud Run entrypoint is a POSIX shell script")
class CloudRunEndToEndTests(unittest.TestCase):
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

        gcloud = root / "fake-gcloud"
        gcloud.write_text(FAKE_GCLOUD.replace("PYTHON", sys.executable, 1),
                          encoding="utf-8")
        gcloud.chmod(0o755)
        cls.state = root / "gcloud-state"

        profiles = root / "profiles"
        profiles.mkdir()
        (profiles / "demo@cloudrun.json").write_text(json.dumps({
            "name": "demo@cloudrun", "slot": "light", "timeout": "5m",
            "cloudrun": {"base_image": "python:3.12-slim"},
        }), encoding="utf-8")
        cls.config_path = root / "config.json"
        cls.config_path.write_text(json.dumps({
            "token": TOKEN,
            "data_dir": str(root / "data"),
            "profiles_dir": str(profiles),
            "hub_url": f"http://127.0.0.1:{cls.port}",
            "hub_bind": "127.0.0.1",
            "hub_port": cls.port,
            "janitor_interval": 0.3,
            "hosts": {"cloudrun": {
                "slots": {"light": 2, "heavy": 1},
                "provider": "cloudrun",
                "cloudrun": {
                    "project": "demo-project",
                    "gcloud": str(gcloud),
                    "hub_url": f"http://127.0.0.1:{cls.port}",
                    "reconcile_interval": "1s",
                    "timeout_grace": "2s",
                },
            }},
            "exec_host": "cloudrun",
            "use_systemd": False,
        }, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "FAKE_GCLOUD_STATE": str(cls.state),
            "FAKE_ENTRYPOINT": str(ENTRYPOINT),
            # What Cloud Run injects from Secret Manager / the job definition.
            "CALLTABLE_TOKEN": TOKEN,
            "CALLTABLE_WORK_DIR": str(root / "cloudrun-work"),
        })
        env.pop("TS_AUTHKEY", None)
        # 子进程的 python3 必须是 3.12+；把当前解释器放到 PATH 最前面。
        shim = root / "bin"
        shim.mkdir()
        (shim / "python3").symlink_to(sys.executable)
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        cls.env = env
        cls.hub_log = (root / "hub.log").open("w", encoding="utf-8")
        cls.hub = subprocess.Popen(
            [sys.executable, "-m", "calltable", "hub", "--config",
             str(cls.config_path)],
            cwd=str(REPO_ROOT), env=env, stdout=cls.hub_log,
            stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                cls._api("/api/health")
                break
            except Exception:
                if cls.hub.poll() is not None:
                    raise RuntimeError("hub exited:\n" + tail(root / "hub.log"))
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        if cls.hub.poll() is None:
            cls.hub.terminate()
            try:
                cls.hub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.hub.kill()
        cls.hub_log.close()
        for pid_file in cls.state.glob("*.pid") if cls.state.exists() else []:
            try:
                os.killpg(int(pid_file.read_text()), 9)
            except OSError:
                pass
        cls.tmp.cleanup()

    @classmethod
    def _api(cls, path):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}",
            headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"null")

    def _diagnostics(self) -> str:
        logs = [f"hub log:\n{tail(self.root / 'hub.log')}"]
        for log in sorted(self.state.glob("*.log")) if self.state.exists() else []:
            logs.append(f"{log.name}:\n{tail(log)}")
        return "\n".join(logs)

    def test_job_runs_through_a_cloud_run_execution(self):
        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path), "--host", "cloudrun",
             "--project", "demo", "--repo", str(self.repo), "--wait",
             "--timeout", "3m", "--",
             sys.executable, "-c",
             "import os; print('hello from', os.environ['CALLTABLE_HOST']);"
             "print(open('README.md').read().strip());"
             "print('token in job env:', 'CALLTABLE_TOKEN' in os.environ)"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=240)
        self.assertEqual(cli.returncode, 0,
                         cli.stdout + cli.stderr + self._diagnostics())
        job_id = job_id_from(cli.stdout)
        job = self._api(f"/api/jobs/{job_id}")
        self.assertEqual(job["status"], "succeeded", self._diagnostics())
        self.assertEqual(job["host"], "cloudrun")
        self.assertFalse(job["worker"].startswith("pending:"))
        self.assertIn("hello from cloudrun", cli.stdout)
        self.assertIn("demo", cli.stdout)
        # 执行器靠环境变量里的 token 连上了 hub（配置文件里 token 为空），
        # 但任务命令看不到它。
        self.assertIn("token in job env: False", cli.stdout)

        calls = [json.loads(line) for line in
                 (self.state / "calls.log").read_text().splitlines()]
        execute = [c for c in calls if c[:3] == ["run", "jobs", "execute"]]
        self.assertEqual(len(execute), 1)
        self.assertEqual(execute[0][3], "ct-demo-light")
        self.assertIn(f"CALLTABLE_JOB_ID={job_id}",
                      next(a for a in execute[0] if a.startswith("--update-env-vars=")))

        # execution 结束后 provider 把它对账掉，花费有记录。
        deadline = time.time() + 30
        summary = {}
        while time.time() < deadline:
            summary = self._api("/api/cloudrun")["hosts"]["cloudrun"]
            if summary["active_executions"] == 0:
                break
            time.sleep(0.5)
        self.assertEqual(summary["active_executions"], 0, self._diagnostics())
        self.assertGreater(summary["month_spend_usd"], 0)
        self.assertFalse(summary["over_budget"])

    def _run_readme_job(self):
        return subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path), "--host", "cloudrun",
             "--project", "demo", "--repo", str(self.repo), "--wait",
             "--timeout", "3m", "--",
             sys.executable, "-c", "print(open('README.md').read().strip())"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=240)

    def _wait_executions_gone(self):
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._api("/api/cloudrun")["hosts"]["cloudrun"]["active_executions"] == 0:
                return
            time.sleep(0.5)
        self.fail("executions still active\n" + self._diagnostics())

    def test_second_job_in_a_fresh_container_gets_the_full_bundle(self):
        """每次 execution 都是全新容器：镜像是空的。

        hub 已有项目历史后，客户端只上传增量 bundle；空镜像吃不下它，执行器
        必须改取整包。回归：整包曾被当成「续传」追加到增量包后面，任务报
        ``Repository lacks these prerequisite commits``。
        """
        first = self._run_readme_job()
        self.assertEqual(first.returncode, 0,
                         first.stdout + first.stderr + self._diagnostics())
        self._wait_executions_gone()

        (self.repo / "README.md").write_text("demo second\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "second")
        # 真实的 Cloud Run 容器不保留上一次的镜像与 runs。
        shutil.rmtree(Path(self.env["CALLTABLE_WORK_DIR"]), ignore_errors=True)

        second = self._run_readme_job()
        self.assertEqual(second.returncode, 0,
                         second.stdout + second.stderr + self._diagnostics())
        self.assertIn("demo second", second.stdout)
        job = self._api(f"/api/jobs/{job_id_from(second.stdout)}")
        self.assertEqual(job["status"], "succeeded", self._diagnostics())


if __name__ == "__main__":
    unittest.main()
