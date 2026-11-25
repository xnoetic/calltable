"""执行器：终态上报重试与 pending 重放（B1）、准备阶段取消/超时（I8）、
产物失败不改判（B3）、镜像路径约束（I4）、启动器约束（A10）。"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from calltable import executor as executor_module
from calltable.client import HubError
from calltable.executor import Aborted, Executor, LogPump
from tests.helpers import make_config


class FakeClient:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[str] = []
        self.finish_failures = 0
        self.claim_job = None
        self.claims: list[dict] = []
        self.cancel_jobs: set[str] = set()
        self.on_download = None

    def post_json(self, path, body=None):
        if path.endswith("/finish") and self.finish_failures > 0:
            self.finish_failures -= 1
            raise HubError("finish rejected")
        if path == "/api/exec/claim":
            self.claims.append(body or {})
            return self.claim_job
        self.posts.append((path, body))
        return {"ok": True}

    def get_json(self, path, params=None):
        return {"cancel_requested": path.rstrip("/").split("/")[-1] in self.cancel_jobs}

    def download(self, path, dest, params=None):
        self.downloads.append(path)
        if self.on_download:
            self.on_download(path)
        Path(dest).write_bytes(b"bundle")
        return Path(dest)

    def upload_file(self, path, file_path, headers=None):
        self.uploads.append((path, str(file_path)))
        return {}

    def request(self, *args, **kwargs):
        raise HubError("log upload failed")


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.client = FakeClient()
        self.exec = Executor(self.config, client=self.client)

    def tearDown(self):
        self.exec.stop()
        self.tmp.cleanup()

    def test_supervise_counts_time_already_spent(self):
        class Tree:
            code = None
            soft_limit_exceeded = False

            def poll(self):
                return self.code

            def terminate(self, grace):
                self.code = -9

        tree = Tree()
        status = self.exec._supervise(
            {"timeout": 10, "started": __import__("time").time() - 60},
            tree, "job-old")
        self.assertEqual(status[0], "timeout")
        self.assertEqual(tree.code, -9)

    def test_artifact_scan_matches_and_prunes_node_modules(self):
        work = self.root / "scan" / "work"
        (work / "tests" / "A" / "TestResults").mkdir(parents=True)
        (work / "tests" / "A" / "TestResults" / "a.trx").write_text("x")
        (work / "artifacts" / "test-results").mkdir(parents=True)
        (work / "artifacts" / "test-results" / "b.trx").write_text("y")
        (work / "node_modules" / "pkg" / "TestResults").mkdir(parents=True)
        (work / "node_modules" / "pkg" / "TestResults" / "n.trx").write_text("z")
        notes = self.exec._collect_artifacts(
            "job-scan", work, ["**/TestResults/**/*.trx",
                               "artifacts/test-results/**/*.trx"])
        uploaded = sorted(__import__("os").path.basename(path)
                          for _, path in self.client.uploads)
        self.assertEqual(uploaded, ["a.trx", "b.trx"])
        self.assertEqual(notes, [])

    def test_artifact_scan_is_bounded(self):
        work = self.root / "bounded" / "work"
        (work / "tests" / "TestResults").mkdir(parents=True)
        (work / "tests" / "TestResults" / "a.trx").write_text("x")
        with mock.patch.object(executor_module, "ARTIFACT_SCAN_SECONDS", 0):
            notes = self.exec._collect_artifacts(
                "job-bounded", work, ["**/*.trx"])
        self.assertTrue(any("truncated" in note for note in notes), notes)

    def test_startup_reclaims_orphan_job(self):
        run_dir = self.exec.runs_root / "job-orphan"
        run_dir.mkdir(parents=True)
        (run_dir / ".running").write_text("previous-worker", encoding="utf-8")
        (run_dir / ".pid").write_text("999999", encoding="utf-8")
        with mock.patch.object(self.exec, "_pid_matches", return_value=True),                 mock.patch.object(self.exec, "_kill_tree") as kill,                 mock.patch.object(self.exec, "_schedule_cleanup"):
            self.exec._reclaim_orphans()
        kill.assert_called_once_with(999999, "job-orphan")
        self.assertIn("/api/exec/jobs/job-orphan/finish", [p for p, _ in self.client.posts])
        self.assertFalse((run_dir / ".running").exists())

    # ------------------------------------------------------------ B1
    def test_finish_retries_then_succeeds(self):
        self.client.finish_failures = 3
        with mock.patch("calltable.executor.time.sleep", return_value=None):
            self.assertTrue(self.exec._finish("job-1", {"status": "succeeded",
                                                        "exit_code": 0}))
        self.assertEqual([p for p, _ in self.client.posts],
                         ["/api/exec/jobs/job-1/finish"])
        self.assertFalse(list(self.exec.pending_dir.glob("*.json")))

    def test_finish_exhaustion_writes_pending_and_replay_succeeds(self):
        self.client.finish_failures = 999
        with mock.patch.object(executor_module, "FINISH_BUDGET", 0), \
                mock.patch("calltable.executor.time.sleep", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(self.exec._finish("job-2", {"status": "failed",
                                                         "exit_code": 7,
                                                         "error": "boom"}))
        self.assertIn("queued for replay", stderr.getvalue())
        pending = self.exec.pending_dir / "job-2.json"
        self.assertTrue(pending.exists())
        self.assertEqual(json.loads(pending.read_text())["exit_code"], 7)

        self.client.finish_failures = 0
        self.assertEqual(self.exec.replay_pending(), 1)
        self.assertFalse(pending.exists())
        self.assertEqual(self.client.posts[-1],
                         ("/api/exec/jobs/job-2/finish",
                          {"status": "failed", "exit_code": 7, "error": "boom"}))

    def test_replay_pending_keeps_file_when_hub_still_down(self):
        pending = self.exec.pending_dir / "job-3.json"
        pending.write_text(json.dumps({"status": "succeeded", "exit_code": 0}))
        self.client.finish_failures = 99
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.exec.replay_pending(), 0)
        self.assertTrue(pending.exists())

    # ------------------------------------------------------------ I8
    def test_cancel_during_preparation_aborts_before_launch(self):
        job = {"id": "job-cancel", "project": "demo", "sha": "a" * 40,
               "timeout": 60, "profile_data": {}, "cmd": ["true"],
               "bundle_url": "/api/jobs/job-cancel/bundle"}
        self.client.on_download = lambda path: self.exec._cancel_flags.update(
            {"job-cancel": True})
        self.exec._run_job(job)
        finish = [body for path, body in self.client.posts
                  if path.endswith("/finish")][-1]
        self.assertEqual(finish["status"], "cancelled")
        self.assertIn("preparation", finish["error"])

    def test_timeout_during_preparation_aborts(self):
        job = {"id": "job-timeout", "project": "demo", "sha": "b" * 40,
               "timeout": 0, "profile_data": {}, "cmd": ["true"],
               "bundle_url": ""}
        self.exec._run_job(job)
        finish = [body for path, body in self.client.posts
                  if path.endswith("/finish")][-1]
        self.assertEqual(finish["status"], "timeout")

    def test_checkpoint_prefers_cancel_over_deadline(self):
        self.exec._cancel_flags["x"] = True
        with self.assertRaises(Aborted) as ctx:
            self.exec._checkpoint("x", deadline=0)
        self.assertEqual(ctx.exception.status, "cancelled")

    # ------------------------------------------------------------ B3
    def test_artifact_upload_failure_is_annotated_not_fatal(self):
        work = self.root / "runs" / "j1" / "work"
        work.mkdir(parents=True)
        (work / "ok.json").write_text("{}", encoding="utf-8")
        self.client.upload_file = mock.Mock(side_effect=HubError("nope"))
        notes = self.exec._collect_artifacts("j1", work, ["*.json"])
        self.assertTrue(notes)
        self.assertIn("artifact upload failed", notes[0])

    def test_artifact_glob_collects_nested_testresults(self):
        work = self.root / "runs" / "j3" / "work"
        nested = work / "tests" / "Example.Mcp.Tests" / "TestResults"
        nested.mkdir(parents=True)
        (nested / "a.trx").write_text("<trx/>", encoding="utf-8")
        (work / "tests" / "Example.Web.Tests" / "TestResults").mkdir(parents=True)
        (work / "tests" / "Example.Web.Tests" / "TestResults" / "b.trx").write_text(
            "<trx/>", encoding="utf-8")
        notes = self.exec._collect_artifacts(
            "j3", work, ["tests/**/TestResults/**/*.trx"])
        self.assertEqual(notes, [])
        uploaded = [path for _, path in self.client.uploads]
        self.assertEqual(len(uploaded), 2)

    def test_artifact_paths_outside_worktree_are_skipped(self):
        work = self.root / "runs" / "j2" / "work"
        work.mkdir(parents=True)
        (self.root / "outside.json").write_text("{}", encoding="utf-8")
        (work / "inside.json").write_text("{}", encoding="utf-8")
        notes = self.exec._collect_artifacts(
            "j2", work, ["../outside.json", "inside.json"])
        self.assertIn("pattern rejected", " ".join(notes))
        uploaded = [path for _, path in self.client.uploads]
        self.assertEqual(len(uploaded), 1)
        self.assertTrue(uploaded[0].endswith("inside.json"))

    # ------------------------------------------------------------ I4 / A10
    def test_mirror_path_must_stay_inside_root(self):
        with self.assertRaises(RuntimeError):
            self.exec._mirror_for("../evil")
        with self.assertRaises(RuntimeError):
            self.exec._mirror_for("bad/name")
        path = self.exec._mirror_for("demo")
        self.assertTrue(str(path).startswith(str(self.config.mirror_root.resolve())))

    def test_launcher_runs_profile_setup_before_the_command(self):
        run_dir = self.root / "run-setup"
        run_dir.mkdir()
        script = self.exec._write_launcher(
            run_dir, {"cmd": ["echo", "job"]},
            {"setup": ["cd web && pnpm install"]}, self.root)
        content = script.read_text(encoding="utf-8")
        self.assertLess(content.index("pnpm install"), content.index("echo job"))
        self.assertIn("umask 077", content)

    def test_runs_dir_is_tightened_on_posix(self):
        if __import__("os").name == "nt":
            self.skipTest("posix permissions only")
        import os as os_module
        runs = self.root / "perm" / "runs"
        runs.mkdir(parents=True)
        os_module.chmod(runs.parent, 0o775)
        os_module.chmod(runs, 0o770)
        self.exec._tighten_private_dirs_for(runs)
        self.assertEqual(runs.stat().st_mode & 0o777, 0o700)
        self.assertEqual(runs.parent.stat().st_mode & 0o777, 0o755)

    def test_cmd_launcher_is_rejected_on_windows(self):
        if __import__("os").name != "nt":
            self.skipTest("windows-only launcher rule")
        self.exec.config.raw["shell"] = "cmd.exe"
        with self.assertRaises(RuntimeError) as ctx:
            self.exec._write_launcher(self.root / "run", {"cmd": ["true"]}, {},
                                      self.root)
        self.assertIn("Git Bash", str(ctx.exception))

    # ------------------------------------------------------------ B2
    def test_systemd_probe_failure_falls_back_to_direct(self):
        self.config.raw.pop("use_systemd", None)
        with mock.patch("calltable.executor.proc.probe_systemd",
                        return_value=(False, "Failed to connect to bus")):
            ex = Executor(self.config, client=self.client)
        self.assertFalse(ex.use_systemd)
        self.assertIn("probe failed", ex.systemd_reason)

    def test_systemd_probe_success_enables_scope(self):
        self.config.raw.pop("use_systemd", None)
        with mock.patch("calltable.executor.proc.probe_systemd",
                        return_value=(True, "ok")):
            ex = Executor(self.config, client=self.client)
        self.assertTrue(ex.use_systemd)
        self.assertEqual(ex.systemd_reason, "probe ok")

    def test_systemd_explicitly_disabled_by_config(self):
        self.config.raw["use_systemd"] = False
        ex = Executor(self.config, client=self.client)
        self.assertFalse(ex.use_systemd)
        self.assertEqual(ex.systemd_reason, "disabled by config")

    # ------------------------------------------------------------ 构建环境
    def test_env_has_build_defaults_and_job_identity(self):
        job = {"id": "job-env", "project": "p", "host": "runner-a",
               "submitter": "s", "sha": "a" * 40, "dirty": False}
        tmp = self.root / "tmp-env"
        work = self.root / "work-env"
        env = self.exec._build_env(job, {}, tmp, work)
        self.assertEqual(env["MSBUILDDISABLENODEREUSE"], "1")
        self.assertEqual(env["DOTNET_CLI_USE_MSBUILD_SERVER"], "0")
        self.assertEqual(env["UseSharedCompilation"], "false")
        self.assertEqual(env["DOTNET_CLI_TELEMETRY_OPTOUT"], "1")
        self.assertEqual(env["DOTNET_NOLOGO"], "1")
        self.assertEqual(env["TMPDIR"], str(tmp))
        self.assertEqual(env["TMP"], str(tmp))
        self.assertEqual(env["TEMP"], str(tmp))
        self.assertEqual(env["CALLTABLE_JOB_ID"], "job-env")
        self.assertEqual(env["CALLTABLE_HOST"], "runner-a")

    def test_env_drops_hub_credentials(self):
        # Cloud Run 上 token 由 Secret Manager 注入容器环境：任务命令不能看到，
        # 但执行器自己还要用（配置文件里没有 token 时回退到环境变量）。
        job = {"id": "job-secret", "project": "p", "host": "runner-a"}
        secrets = {"CALLTABLE_TOKEN": "hub-secret", "TS_AUTHKEY": "tskey-secret",
                   "CALLTABLE_KEEP_ME": "kept"}
        with mock.patch.dict(os.environ, secrets):
            env = self.exec._build_env(job, {}, self.root / "tmp",
                                       self.root / "work")
            self.assertNotIn("CALLTABLE_TOKEN", env)
            self.assertNotIn("TS_AUTHKEY", env)
            self.assertNotIn("hub-secret", env.values())
            self.assertNotIn("tskey-secret", env.values())
            self.assertEqual(env["CALLTABLE_KEEP_ME"], "kept")
            # 只动传给任务的副本，执行器进程自己的环境不变。
            self.assertEqual(os.environ["CALLTABLE_TOKEN"], "hub-secret")
            self.assertEqual(self.config.token_value, "hub-secret")

    def test_profile_env_can_still_set_credential_names(self):
        job = {"id": "job-explicit", "project": "p", "host": "runner-a"}
        profile = {"env": {"CALLTABLE_TOKEN": "from-profile",
                           "TS_AUTHKEY": "profile-key"}}
        with mock.patch.dict(os.environ, {"CALLTABLE_TOKEN": "hub-secret",
                                          "TS_AUTHKEY": "tskey-secret"}):
            env = self.exec._build_env(job, profile, self.root / "tmp",
                                       self.root / "work")
        self.assertEqual(env["CALLTABLE_TOKEN"], "from-profile")
        self.assertEqual(env["TS_AUTHKEY"], "profile-key")

    def test_launcher_raises_task_oom_score(self):
        self.exec.config.raw["task_oom_score_adj"] = 650
        run_dir = self.root / "run-oom"
        run_dir.mkdir()
        script = self.exec._write_launcher(
            run_dir, {"cmd": ["true"]}, {}, self.root)
        content = script.read_text(encoding="utf-8")
        self.assertIn(executor_module.OOM_SCORE_ADJ_PATH, content)
        self.assertIn("650", content)

    def test_launcher_guards_the_oom_score_write(self):
        # 只在 procfs 入口存在且可写时才写：Windows / Git Bash 上没有
        # /proc，旧写法 `echo ... > /proc/... 2>/dev/null` 仍会把重定向失败
        # 的报错打进每个任务的日志。
        self.exec.config.raw["task_oom_score_adj"] = 650
        run_dir = self.root / "run-oom-guard"
        run_dir.mkdir()
        script = self.exec._write_launcher(
            run_dir, {"cmd": ["true"]}, {}, self.root)
        content = script.read_text(encoding="utf-8")
        self.assertIn(f"[ -w {executor_module.OOM_SCORE_ADJ_PATH} ]", content)

    def _run_launcher_with_oom_path(self, name: str, target: str) -> tuple:
        self.exec.config.raw["task_oom_score_adj"] = 640
        run_dir = self.root / name
        run_dir.mkdir()
        with mock.patch.object(executor_module, "OOM_SCORE_ADJ_PATH", target):
            script = self.exec._write_launcher(
                run_dir, {"cmd": ["true"]}, {}, self.root)
        import subprocess
        proc = subprocess.run([self.config.shell, str(script)],
                              cwd=str(self.root), capture_output=True,
                              text=True, timeout=60)
        return proc

    def test_launcher_writes_oom_score_when_the_entry_is_writable(self):
        # 用临时文件冒充 /proc/self/oom_score_adj：Linux 上的调整行为不变。
        fake = self.root / "oom_score_adj"
        fake.write_text("0\n", encoding="utf-8")
        proc = self._run_launcher_with_oom_path("run-oom-writable", str(fake))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(fake.read_text(encoding="utf-8").strip(), "640")
        self.assertEqual(proc.stderr, "")

    def test_launcher_is_quiet_when_the_oom_entry_is_missing(self):
        # 非 Linux（或任何没有 procfs 的环境）：不写、也不报错。
        missing = self.root / "no-such-dir" / "oom_score_adj"
        proc = self._run_launcher_with_oom_path("run-oom-missing", str(missing))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertFalse(missing.exists())

    def test_real_oom_score_is_applied_on_linux(self):
        import os as os_module
        import sys as sys_module
        if not sys_module.platform.startswith("linux"):
            self.skipTest("procfs oom_score_adj is Linux-only")
        if not os_module.access("/proc/self/oom_score_adj", os_module.W_OK):
            self.skipTest("/proc/self/oom_score_adj is not writable here")
        self.exec.config.raw["task_oom_score_adj"] = 620
        run_dir = self.root / "run-oom-real"
        run_dir.mkdir()
        script = self.exec._write_launcher(
            run_dir, {"cmd": ["cat", "/proc/self/oom_score_adj"]}, {},
            self.root)
        import subprocess
        proc = subprocess.run([self.config.shell, str(script)],
                              cwd=str(self.root), capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "620")

    # ------------------------------------------------------------ 基线 ref
    def test_env_exposes_baseline_contract(self):
        job = {"id": "j1", "project": "p", "host": "h", "submitter": "s",
               "sha": "a" * 40, "base_sha": "b" * 40, "base_ref": "origin/main",
               "base_ref_sha": "c" * 40, "dirty": True}
        env = self.exec._build_env(job, {}, self.root / "tmp", self.root / "work")
        self.assertEqual(env["CALLTABLE_SHA"], "a" * 40)
        self.assertEqual(env["CALLTABLE_BASE_SHA"], "b" * 40)
        self.assertEqual(env["CALLTABLE_BASE_REF_SHA"], "c" * 40)
        self.assertEqual(env["CALLTABLE_DIRTY"], "1")

    def test_env_marks_clean_tree(self):
        job = {"id": "j2", "project": "p", "host": "h", "submitter": "s",
               "sha": "a" * 40, "base_sha": "a" * 40,
               "base_ref_sha": "a" * 40, "dirty": False}
        env = self.exec._build_env(job, {}, self.root / "tmp", self.root / "work")
        self.assertEqual(env["CALLTABLE_DIRTY"], "0")

    # ------------------------------------------------------------ I5
    def test_janitor_keeps_running_dirs_and_prunes_worktrees(self):
        runs = self.config.runs_root
        running = runs / "job-running"
        running.mkdir(parents=True)
        (running / ".running").write_text(self.exec.worker_id)
        expired = runs / "job-expired"
        expired.mkdir(parents=True)
        (expired / ".retention").write_text("0")
        fresh = runs / "job-fresh"
        fresh.mkdir(parents=True)
        (fresh / ".retention").write_text(str(__import__("time").time() + 3600))

        self.exec.janitor()

        self.assertTrue(running.exists(), "running job dir must be kept")
        self.assertFalse(expired.exists(), "expired job dir must be removed")
        self.assertTrue(fresh.exists(), "unexpired job dir must be kept")

    # ------------------------------------------------------------ A1 / A7
    def test_once_returns_after_one_empty_claim(self):
        once = Executor(self.config, once=True, client=self.client)
        with contextlib.redirect_stdout(io.StringIO()):
            once.run()
        paths = [path for path, _ in self.client.posts]
        self.assertIn("/api/hosts/heartbeat", paths)
        self.assertEqual(len(self.client.claims), 1)
        self.assertEqual(self.client.claims[0]["host"], "h1")
        self.assertTrue(self.client.claims[0]["worker"])

    # ------------------------------------------------------------ 磁盘水位
    def test_heartbeat_reports_disk_memory_and_load(self):
        self.client.claim_job = None
        self.exec.disk_status = lambda force=False: executor_module.disk.DiskStatus(
            free=120 * executor_module.disk.GB,
            runs=40 * executor_module.disk.GB,
            min_free_gb=100, runs_max_gb=300, low=False)
        self.assertTrue(self.exec.heartbeat())
        body = [b for path, b in self.client.posts
                if path == "/api/hosts/heartbeat"][-1]
        system = body["system"]
        self.assertEqual(system["disk_free_gb"], 120.0)
        self.assertEqual(system["min_free_gb"], 100)
        self.assertFalse(system["disk_low"])
        self.assertIn("mem_total_gb", system)
        self.assertIn("load1", system)

    def test_claim_loop_stops_while_disk_is_low(self):
        self.exec.disk_status = lambda force=False: executor_module.disk.DiskStatus(
            free=1024, runs=0, min_free_gb=100, runs_max_gb=0, low=True,
            reason="free 0.0GB < 100GB")
        with mock.patch.object(executor_module, "print") as printer:
            self.exec._claim_loop()
        self.assertEqual(self.client.claims, [])
        self.assertTrue(printer.called)

    def test_disk_watermark_config_is_used_by_cleanup(self):
        self.config.raw["min_free_gb"] = 100
        self.config.raw["runs_max_gb"] = 300
        seen = {}

        def fake_enforce(runs_root, *, min_free_gb, runs_max_gb, protected,
                         cache=None, scan_budget=None):
            seen.update(min_free_gb=min_free_gb, runs_max_gb=runs_max_gb)
            return executor_module.disk.DiskStatus(min_free_gb=min_free_gb,
                                                  runs_max_gb=runs_max_gb)

        with mock.patch.object(executor_module.disk, "enforce",
                               side_effect=fake_enforce):
            self.exec.cleanup_disk()
        self.assertEqual(seen, {"min_free_gb": 100, "runs_max_gb": 300})

    def test_cleanup_disk_is_single_flight(self):
        # janitor 线程和按需清理线程曾经同时在删同一批目录。
        calls = []

        def fake_enforce(runs_root, **kwargs):
            calls.append(1)
            return executor_module.disk.DiskStatus()

        with mock.patch.object(executor_module.disk, "enforce",
                               side_effect=fake_enforce):
            self.assertTrue(self.exec._disk_cleanup_running.acquire(blocking=False))
            try:
                self.exec.cleanup_disk()  # 另一份清理还在跑：直接返回
            finally:
                self.exec._disk_cleanup_running.release()
            self.assertEqual(calls, [])
            self.exec.cleanup_disk()
        self.assertEqual(calls, [1])

    def test_cleanup_disk_reports_undeletable_dirs_once(self):
        def fake_enforce(runs_root, **kwargs):
            return executor_module.disk.DiskStatus(
                low=True, reason="runs 334.7GB > 300GB",
                undeletable=["job-stuck"])

        with mock.patch.object(executor_module.disk, "enforce",
                               side_effect=fake_enforce), \
                mock.patch.object(executor_module, "print") as printer:
            self.exec.cleanup_disk()
            self.exec.cleanup_disk()
        messages = [" ".join(str(arg) for arg in call.args)
                    for call in printer.call_args_list]
        stuck = [text for text in messages if "job-stuck" in text]
        self.assertEqual(len(stuck), 1)
        self.assertIn("cannot fully remove", stuck[0])

    def test_heartbeat_measurement_never_deletes_run_dirs(self):
        run_dir = self.root / "runs" / "old-run"
        (run_dir / "work").mkdir(parents=True)
        (run_dir / "work" / "f.bin").write_bytes(b"x" * 1024)
        self.config.raw["min_free_gb"] = 10 ** 6  # 永远不够
        with mock.patch.object(executor_module.disk, "enforce") as enforce:
            status = self.exec.disk_status(force=True)
        enforce.assert_not_called()
        self.assertTrue(status.low)
        self.assertTrue(run_dir.exists())

    def test_claim_loop_schedules_background_cleanup_when_low(self):
        self.exec.disk_status = lambda force=False: executor_module.disk.DiskStatus(
            free=1, runs=0, min_free_gb=100, runs_max_gb=0, low=True,
            reason="free 0.0GB < 100GB")
        started = []
        with mock.patch.object(self.exec, "request_disk_cleanup",
                               side_effect=lambda: started.append(1) or True),                 mock.patch.object(executor_module, "print"):
            self.exec._claim_loop()
        self.assertEqual(started, [1])
        self.assertEqual(self.client.claims, [])

    def test_cleanup_marker_records_completion_time(self):
        run_dir = self.root / "runs" / "job-done"
        run_dir.mkdir(parents=True)
        self.exec._schedule_cleanup(run_dir, "failed")
        finished = float((run_dir / executor_module.disk.FINISHED_MARKER)
                         .read_text())
        retention = float((run_dir / ".retention").read_text())
        self.assertAlmostEqual(retention - finished,
                               self.config.retention_failure, delta=5)

    # ------------------------------------------------------------ 软内存上限
    def test_supervise_kills_tree_when_soft_memory_limit_is_exceeded(self):
        class Tree:
            code = None
            soft_limit_exceeded = False
            killed = False

            def poll(self):
                return self.code

            def terminate(self, grace):
                self.killed = True
                self.code = -9

        tree = Tree()

        def flips_exceeded(seconds):
            tree.soft_limit_exceeded = True
            return 0

        with mock.patch("calltable.executor.time.sleep",
                        side_effect=flips_exceeded):
            status, error = self.exec._supervise({"timeout": 60}, tree, "job-mem")
        self.assertEqual(status, "error")
        self.assertEqual(error, "memory limit exceeded (soft, direct mode)")
        self.assertTrue(tree.killed)

    def test_log_pump_reports_upload_failure_to_stderr(self):
        pump = LogPump(self.client, "job-log", None)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with mock.patch("calltable.executor.time.sleep", return_value=None):
                pump._post(b"hello")
        self.assertIn("log upload", stderr.getvalue())


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def run_git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class GitRelayFetchTests(unittest.TestCase):
    """Cloud Run + cloudrun.git：执行器先从远端仓库（这里是本地裸仓库）取
    hub 推过去的临时 ref，到手就不下载 hub bundle；失败照旧走 bundle。"""

    TOKEN = "ghp-test-token-should-never-leak"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.client = FakeClient()
        self.exec = Executor(self.config, client=self.client)
        self.src = self.root / "src"
        self.src.mkdir()
        run_git(self.src, "init", "-q", "-b", "main")
        (self.src / "a.txt").write_text("one\n", encoding="utf-8")
        run_git(self.src, "add", "-A")
        run_git(self.src, "commit", "-qm", "one")
        self.base = run_git(self.src, "rev-parse", "HEAD")
        (self.src / "a.txt").write_text("two\n", encoding="utf-8")
        run_git(self.src, "commit", "-qam", "two")
        self.sha = run_git(self.src, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        run_git(self.root, "init", "-q", "--bare", str(self.remote))
        self.refs = "refs/calltable/jobs/job-relay"
        # 真 bundle：回退到 hub 时 FakeClient 交出它。
        self.bundle = self.root / "full.bundle"
        run_git(self.src, "bundle", "create", str(self.bundle), "main")
        self.client.download = self._download

    def tearDown(self):
        self.exec.stop()
        self.tmp.cleanup()

    def _download(self, path, dest, params=None):
        self.client.downloads.append(path)
        shutil.copyfile(self.bundle, dest)
        return Path(dest)

    def push_refs(self, head=None, base=None):
        refspecs = [f"{head or self.sha}:{self.refs}/head"]
        if base:
            refspecs.append(f"{base}:{self.refs}/base")
        run_git(self.src, "push", "-q", str(self.remote), *refspecs)

    def run_job(self, env, **job_fields):
        job = {"id": "job-relay", "project": "demo", "host": "h1",
               "sha": self.sha,
               "timeout": 120, "profile_data": {}, "cmd": ["true"],
               "bundle_url": "/api/jobs/job-relay/bundle"}
        job.update(job_fields)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            self.exec._run_job(job)
        finish = [body for path, body in self.client.posts
                  if path.endswith("/finish")][-1]
        return finish, out.getvalue()

    def relay_env(self, url=None, token=None):
        env = {"CALLTABLE_GIT_URL": str(url or self.remote),
               "CALLTABLE_GIT_REFS": self.refs}
        if token:
            env["CALLTABLE_GIT_TOKEN"] = token
        return env

    def test_commit_from_the_remote_skips_the_hub_bundle(self):
        self.push_refs(base=self.base)
        finish, out = self.run_job(self.relay_env(), base_ref="origin/main",
                                   base_ref_sha=self.base)
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertEqual(self.client.downloads, [])
        self.assertIn("git relay: fetched", out)
        mirror = self.exec._mirror_for("demo")
        self.assertEqual(run_git(mirror, "rev-parse", "refs/calltable/relay/head"),
                         self.sha)

    def test_missing_base_alone_is_fetched_from_the_hub(self):
        self.push_refs()  # 只有 head：基线仍向 hub 要
        finish, _ = self.run_job(self.relay_env(), base_ref="origin/main",
                                 base_ref_sha="f" * 40)
        # 基线 sha 不存在于任何地方：hub 回退被调用（bundle 里也没有），任务出错，
        # 但关键是只为基线下载过一次，没有下载 head 的 bundle。
        self.assertEqual(len(self.client.downloads), 1, self.client.downloads)
        self.assertIn("sha=" + "f" * 40, self.client.downloads[0])
        self.assertEqual(finish["status"], "error")

    def test_fetch_failure_falls_back_to_hub_bundles(self):
        finish, out = self.run_job(self.relay_env(url=self.root / "missing.git"))
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertEqual(self.client.downloads, ["/api/jobs/job-relay/bundle"])
        self.assertIn("git relay fetch failed, falling back to hub bundles", out)

    def test_commit_missing_from_the_refs_falls_back(self):
        self.push_refs(head=self.base)  # 远端 ref 指向别的提交
        finish, out = self.run_job(self.relay_env())
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertEqual(self.client.downloads, ["/api/jobs/job-relay/bundle"])
        self.assertIn("not in " + self.refs, out)

    def test_without_relay_env_nothing_changes(self):
        self.push_refs()
        env = {"CALLTABLE_GIT_URL": "", "CALLTABLE_GIT_REFS": ""}
        with mock.patch.object(Executor, "_fetch_git_relay") as fetch:
            finish, out = self.run_job(env)
        fetch.assert_not_called()
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertEqual(self.client.downloads, ["/api/jobs/job-relay/bundle"])
        self.assertNotIn("git relay", out)

    def test_token_goes_through_env_config_only(self):
        self.push_refs()
        calls = []
        real_run = subprocess.run

        def spy(argv, *args, **kwargs):
            calls.append((list(argv), dict(kwargs.get("env") or {})))
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(executor_module.gitops.subprocess, "run",
                               side_effect=spy):
            finish, out = self.run_job(self.relay_env(token=self.TOKEN))
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertEqual(self.client.downloads, [])
        fetches = [(argv, env) for argv, env in calls if "fetch" in argv]
        self.assertEqual(len(fetches), 1)
        argv, env = fetches[0]
        self.assertNotIn(self.TOKEN, " ".join(argv))
        basic = base64.b64encode(
            f"x-access-token:{self.TOKEN}".encode()).decode()
        self.assertNotIn(basic, " ".join(argv))
        index = int(env["GIT_CONFIG_COUNT"]) - 1
        self.assertEqual(env[f"GIT_CONFIG_KEY_{index}"], "http.extraheader")
        self.assertEqual(env[f"GIT_CONFIG_VALUE_{index}"],
                         f"Authorization: Basic {basic}")
        mirror = self.exec._mirror_for("demo")
        config = (mirror / "config").read_text(encoding="utf-8")
        self.assertNotIn(self.TOKEN, config)
        self.assertNotIn(basic, config)
        self.assertNotIn("extraheader", config)
        self.assertNotIn(self.TOKEN, out)

    def test_token_is_scrubbed_from_failure_logs(self):
        # 让 git 的报错带上 token：URL 里故意含 token 文本，日志里必须被打码。
        url = self.root / f"missing-{self.TOKEN}.git"
        finish, out = self.run_job(self.relay_env(url=url, token=self.TOKEN))
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertIn("git relay fetch failed", out)
        self.assertNotIn(self.TOKEN, out)
        self.assertNotIn(self.TOKEN, json.dumps(finish))

    def fetch_times_out(self):
        """远端卡住：fetch 用完给它的超时后抛 TimeoutExpired。"""
        seen = []
        real = executor_module.gitops.git

        def git(args, *a, **kwargs):
            if "fetch" in args and kwargs.get("timeout") is not None \
                    and "refs/calltable/relay" in args[-1]:
                seen.append(kwargs["timeout"])
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return real(args, *a, **kwargs)

        return seen, mock.patch.object(executor_module.gitops, "git", side_effect=git)

    def test_fetch_timeout_uses_at_most_half_the_budget(self):
        seen, patch = self.fetch_times_out()
        with patch:
            finish, out = self.run_job(self.relay_env(), timeout=120)
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], 60.0)
        self.assertGreater(seen[0], 50.0)
        # 超时后预算还剩一半：回退到 hub bundle，任务成功而不是超时。
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertEqual(self.client.downloads, ["/api/jobs/job-relay/bundle"])
        self.assertIn("git relay fetch failed", out)
        self.assertIn("TimeoutExpired", out)

    def test_fetch_timeout_is_capped(self):
        seen, patch = self.fetch_times_out()
        with patch:
            finish, _ = self.run_job(self.relay_env(), timeout=6 * 3600)
        self.assertEqual(seen, [executor_module.GIT_RELAY_FETCH_TIMEOUT])
        self.assertLessEqual(executor_module.GIT_RELAY_FETCH_TIMEOUT, 300)
        self.assertEqual(finish["status"], "succeeded", finish)

    @unittest.skipIf(os.name == "nt", "uses the POSIX sleep command")
    def test_hung_fetch_really_falls_back_to_bundles(self):
        # 不联网地模拟卡住的远端：fetch 换成一个长 sleep 的真实子进程，
        # 剩余预算的一半一到就被杀掉，之后仍有时间走 hub bundle。
        seen = []
        real_run = subprocess.run

        def slow(argv, *a, **kwargs):
            if "fetch" in argv and any("refs/calltable/relay" in x for x in argv):
                seen.append(kwargs.get("timeout"))
                return real_run(["sleep", "30"], *a, **{**kwargs, "input": None})
            return real_run(argv, *a, **kwargs)

        with mock.patch.object(executor_module.gitops.subprocess, "run",
                               side_effect=slow):
            started = time.time()
            finish, out = self.run_job(self.relay_env(), timeout=10)
        self.assertLess(time.time() - started, 25)
        self.assertLessEqual(seen[0], 5.0)
        self.assertEqual(finish["status"], "succeeded", finish)
        self.assertIn("git relay fetch failed", out)

    def test_git_token_is_not_in_the_job_env(self):
        job = {"id": "job-secret", "project": "p", "host": "runner-a"}
        with mock.patch.dict(os.environ, {"CALLTABLE_GIT_TOKEN": self.TOKEN}):
            env = self.exec._build_env(job, {}, self.root / "tmp",
                                       self.root / "work")
        self.assertNotIn("CALLTABLE_GIT_TOKEN", env)
        self.assertNotIn(self.TOKEN, env.values())


if __name__ == "__main__":
    unittest.main()
