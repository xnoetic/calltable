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

from tests.helpers import UNSHARE_PID1, can_unshare_pid1
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
    # Optional command prefix (e.g. `unshare ...` to make the entrypoint PID 1).
    prefix = []
    if (state / "launch-prefix.json").exists():
        prefix = json.loads((state / "launch-prefix.json").read_text())
    proc = subprocess.Popen([*prefix, "sh", os.environ["FAKE_ENTRYPOINT"]],
                            env=env, stdout=log, stderr=subprocess.STDOUT,
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

#: 任务脚本：造一个孤儿（子 shell 立刻退出，sleep 被过继给 PID 1），杀掉它，
#: 再看 PID 1 有没有回收它。僵尸在 /proc 里还占着条目，回收后条目消失。
ORPHAN_JOB = r'''
( sleep 300 >/dev/null 2>&1 & echo $! > orphan.pid )
pid=$(cat orphan.pid)
read -r _ _ _ ppid _ < /proc/$pid/stat
echo "pid 1 is: $(tr '\0' ' ' < /proc/1/cmdline)"
echo "orphan adopted by pid $ppid"
kill -9 "$pid"
for _ in $(seq 100); do
  if [ ! -e /proc/$pid ]; then echo "orphan reaped"; exit 0; fi
  sleep 0.1
done
echo "orphan $pid was never reaped: $(cat /proc/$pid/stat)"
exit 9
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
        # 「GitHub」：本地裸仓库。项目 relay 配了 cloudrun.git，代码经它中转。
        cls.remote = root / "github-remote.git"
        git(root, "clone", "-q", "--bare", str(cls.repo), str(cls.remote))
        (profiles / "relay@cloudrun.json").write_text(json.dumps({
            "name": "relay@cloudrun", "slot": "light", "timeout": "5m",
            "cloudrun": {"base_image": "python:3.12-slim",
                         "git": {"url": str(cls.remote)}},
        }), encoding="utf-8")
        # 镜像里预置的镜像目录：默认不存在（其余用例走 $WORK/mirrors），
        # relay 用例临时建出来，模拟 `ct cloudrun image` 烘进去的历史。
        cls.baked = root / "baked-mirrors"
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
            "CALLTABLE_MIRROR_ROOT": str(cls.baked),
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
        # 同一个 hub 上别的用例也会 execute：只看这个任务的那一次。
        execute = [c for c in calls if c[:3] == ["run", "jobs", "execute"]
                   and f"CALLTABLE_JOB_ID={job_id}" in " ".join(c)]
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

    def test_orphans_are_reaped_when_the_executor_is_pid1(self):
        """容器里没有 init，执行器就是 PID 1：任务留下的孤儿必须有人回收。

        用非特权 user + PID namespace 让 entrypoint 真的以 PID 1 起来。回归：
        执行器从不 wait 过继来的子进程，它们退出后一直是僵尸，攒到 execution
        结束（calltable 自己的测试套件跑一遍就留下一百多个）。
        """
        if not can_unshare_pid1():
            self.skipTest("unprivileged user + PID namespaces are not "
                          "available on this host")
        self.state.mkdir(parents=True, exist_ok=True)
        prefix = self.state / "launch-prefix.json"
        prefix.write_text(json.dumps(UNSHARE_PID1), encoding="utf-8")
        self.addCleanup(prefix.unlink, missing_ok=True)

        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path), "--host", "cloudrun",
             "--project", "demo", "--repo", str(self.repo), "--wait",
             "--timeout", "3m", "--", "bash", "-c", ORPHAN_JOB],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=240)
        detail = cli.stdout + cli.stderr + self._diagnostics()
        # 场景确实成立：PID 1 是 calltable 执行器的进程映像，孤儿过继给了它。
        self.assertRegex(cli.stdout, r"pid 1 is: .*-m calltable exec ", detail)
        self.assertIn("orphan adopted by pid 1", cli.stdout, detail)
        self.assertIn("orphan reaped", cli.stdout, detail)
        self.assertEqual(cli.returncode, 0, detail)
        job = self._api(f"/api/jobs/{job_id_from(cli.stdout)}")
        self.assertEqual(job["status"], "succeeded", detail)
        self._wait_executions_gone()

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

    def test_cloudrun_git_relays_code_through_the_remote(self):
        """profile 配了 cloudrun.git：hub 把快照（含未提交改动）推成远端临时
        ref，容器在预置镜像上只取增量，不从 hub 下载 bundle；结束后 ref 被删。"""
        self.baked.mkdir()
        self.addCleanup(shutil.rmtree, self.baked, True)
        git(self.root, "clone", "-q", "--bare", "--single-branch", "--branch",
            "main", str(self.remote), str(self.baked / "relay.git"))
        untracked = self.repo / "relay.txt"
        untracked.write_text("uncommitted relay work\n", encoding="utf-8")
        self.addCleanup(untracked.unlink, True)
        shutil.rmtree(Path(self.env["CALLTABLE_WORK_DIR"]), ignore_errors=True)

        cli = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--config", str(self.config_path), "--host", "cloudrun",
             "--project", "relay", "--repo", str(self.repo), "--wait",
             "--timeout", "3m", "--",
             sys.executable, "-c", "print(open('relay.txt').read().strip())"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True,
            timeout=240)
        self.assertEqual(cli.returncode, 0,
                         cli.stdout + cli.stderr + self._diagnostics())
        self.assertIn("uncommitted relay work", cli.stdout)
        job_id = job_id_from(cli.stdout)
        self.assertEqual(self._api(f"/api/jobs/{job_id}")["status"], "succeeded")

        calls = [json.loads(line) for line in
                 (self.state / "calls.log").read_text().splitlines()]
        execute = [c for c in calls if c[:3] == ["run", "jobs", "execute"]
                   and c[3] == "ct-relay-light"]
        self.assertEqual(len(execute), 1)
        env_arg = next(a for a in execute[0] if a.startswith("--update-env-vars="))
        self.assertIn(f"CALLTABLE_GIT_URL={self.remote}", env_arg)
        self.assertIn(f"CALLTABLE_GIT_REFS=refs/calltable/jobs/{job_id}", env_arg)
        exec_logs = "".join(log.read_text() for log in self.state.glob("ct-relay-*.log"))
        self.assertIn(f"git relay: fetched refs/calltable/jobs/{job_id}", exec_logs,
                      self._diagnostics())
        self.assertNotIn("falling back", exec_logs)
        # 用的是预置镜像（CALLTABLE_MIRROR_ROOT），提交进了它的私有命名空间。
        relayed = git(self.baked / "relay.git", "for-each-ref",
                      "--format=%(refname)", "refs/calltable/relay/")
        self.assertIn("refs/calltable/relay/head", relayed)

        self._wait_executions_gone()
        deadline = time.time() + 30
        remaining = "?"
        while time.time() < deadline:
            remaining = git(self.root, "ls-remote", str(self.remote),
                            "refs/calltable/*")
            if not remaining:
                break
            time.sleep(0.5)
        self.assertEqual(remaining, "", self._diagnostics())


if __name__ == "__main__":
    unittest.main()
