"""Cloud Run provider：预留 + attach、按 worker 对账、花费封顶、execution 对账、
镜像/job 定义命令。全部用假的 Cloud Run API，不调用 gcloud、不产生费用。"""

from __future__ import annotations

import io
import contextlib
import http.client
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from calltable import cloudrun
from calltable import cloudrun_cli
from calltable.client import HubClient
from calltable.cloudrun import CloudRunSettings, Provisioner
from calltable.db import PENDING_WORKER_PREFIX
from calltable.hub import Hub
from calltable.util import now
from tests.helpers import make_config, wait_until

CLOUDRUN = {
    "project": "demo-project",
    "region": "us-central1",
    "service_account": "calltable-exec@demo-project.iam.gserviceaccount.com",
    "hub_url": "http://100.64.0.1:8091",
    "monthly_budget_usd": 60,
    "reconcile_interval": "0s",
    "attach_timeout": "15m",
    "heartbeat_timeout": "15m",
}


def cloud_hosts(**overrides) -> dict:
    settings = dict(CLOUDRUN)
    settings.update(overrides)
    return {
        "h1": {"slots": {"heavy": 1, "light": 2}},
        "cloudrun": {"slots": {"light": 2, "heavy": 1}, "provider": "cloudrun",
                     "labels": ["linux", "cloudrun"], "cloudrun": settings},
    }


class FakeAPI(cloudrun.CloudRunAPI):
    def __init__(self):
        self.runs: list[dict] = []
        self.cancelled: list[str] = []
        self.states: dict[str, dict] = {}
        self.fail_run = False

    def run_job(self, settings, job_name, *, env, task_timeout):
        if self.fail_run:
            raise cloudrun.CloudRunError("quota exceeded")
        name = f"{job_name}-x{len(self.runs) + 1}"
        self.runs.append({"job": job_name, "env": dict(env),
                          "timeout": task_timeout, "execution": name})
        self.states[name] = {"status": {}}
        return name

    def describe_execution(self, settings, execution):
        return self.states.get(execution, {"status": {}})

    def cancel_execution(self, settings, execution):
        self.cancelled.append(execution)

    def complete(self, execution, failed=False):
        self.states[execution] = {"status": {
            "completionTime": "2026-11-25T06:00:00.123456789Z",
            "succeededCount": 0 if failed else 1,
            "failedCount": 1 if failed else 0}}


class BlockingAPI(FakeAPI):
    """describe 卡住不返回，直到测试放行：模拟变慢 / 挂死的 gcloud。"""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.max_active = 0
        self.describes = 0
        self._guard = threading.Lock()

    def describe_execution(self, settings, execution):
        with self._guard:
            self.active += 1
            self.describes += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            self.release.wait(60)
            return super().describe_execution(settings, execution)
        finally:
            with self._guard:
                self.active -= 1


class Base(unittest.TestCase):
    hosts_overrides: dict = {}
    api_class = FakeAPI

    def profile_git(self) -> dict | None:
        """子类返回 profile 的 ``cloudrun.git``；None 表示不配置（功能关闭）。"""
        return None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root, hosts=cloud_hosts(**self.hosts_overrides))
        profile_cloudrun = {"base_image": "mcr.microsoft.com/dotnet/sdk:10.0",
                            "install": ["apt-get update && apt-get install -y jq"],
                            "env": {"DOTNET_NOLOGO": "1"}}
        git_settings = self.profile_git()
        if git_settings is not None:
            profile_cloudrun["git"] = git_settings
        (self.root / "profiles" / "demo@cloudrun.json").write_text(json.dumps({
            "name": "demo@cloudrun", "slot": "light", "timeout": "20m",
            "cloudrun": profile_cloudrun,
            "rules": [{"match": "gate", "slot": "heavy", "timeout": "2h"}],
        }), encoding="utf-8")
        self.api = self.api_class()
        self.hub = Hub(self.config, cloudrun_api=self.api)
        self.store = self.hub.store
        self.provisioner = self.hub.cloudrun

    def tearDown(self):
        self.hub.close()
        self.tmp.cleanup()

    def submit(self, cmd=("echo", "hi"), host="cloudrun", **extra):
        body = {"project": "demo", "host": host, "cmd": list(cmd),
                "client_version": "9.9.9"}
        body.update(extra)
        return self.hub.create_job(body)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        settings = CloudRunSettings.from_host("cloudrun", {"project": "p"})
        self.assertEqual(settings.region, "us-central1")
        self.assertEqual(settings.registry,
                         "us-central1-docker.pkg.dev/p/calltable")
        self.assertEqual(settings.monthly_budget_usd, 120.0)
        self.assertEqual(settings.slot_resources("heavy"), (8.0, 32.0))
        self.assertEqual(settings.slot_resources("light"), (2.0, 8.0))
        self.assertEqual(settings.max_task_timeout, 3 * 3600)
        custom = CloudRunSettings.from_host("cloudrun", {
            "project": "p", "resources": {"heavy": {"memory": "16Gi"}}})
        self.assertEqual(custom.slot_resources("heavy"), (8.0, 16.0))

    def test_project_is_required_and_budget_action_validated(self):
        with self.assertRaises(ValueError):
            CloudRunSettings.from_host("cloudrun", {})
        with self.assertRaises(ValueError):
            CloudRunSettings.from_host("cloudrun", {"project": "p",
                                                    "on_budget_exceeded": "x"})

    def test_cost_has_a_one_minute_minimum(self):
        settings = CloudRunSettings.from_host("cloudrun", {"project": "p"})
        self.assertAlmostEqual(settings.cost(1, 2, 8),
                               60 * (2 * 0.000018 + 8 * 0.000002))
        self.assertAlmostEqual(settings.cost(3600, 8, 32),
                               3600 * (8 * 0.000018 + 32 * 0.000002))

    def test_task_timeout_adds_grace_and_is_capped(self):
        settings = CloudRunSettings.from_host("cloudrun", {"project": "p"})
        self.assertEqual(settings.task_timeout(1200), 1500)
        self.assertEqual(settings.task_timeout(10 * 3600), 3 * 3600)

    def test_names_are_valid_cloud_run_names(self):
        settings = CloudRunSettings.from_host("cloudrun", {"project": "p"})
        self.assertEqual(settings.job_name("My_Project.Web", "heavy"),
                         "ct-my-project-web-heavy")
        self.assertEqual(cloudrun.sanitize_name("9lives"), "x-9lives")
        self.assertLessEqual(len(settings.job_name("a" * 100, "light")), 63)

    def test_memory_parsing(self):
        self.assertEqual(cloudrun.memory_gib("512Mi"), 0.5)
        self.assertEqual(cloudrun.memory_gib("4G"), 4.0)
        self.assertEqual(cloudrun.memory_gib(2), 2.0)
        with self.assertRaises(ValueError):
            cloudrun.memory_gib("lots")

    def test_static_hosts_keep_static_provider(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        config = make_config(Path(tmp.name), hosts=cloud_hosts())
        self.assertEqual(config.hosts["h1"].provider, "static")
        self.assertFalse(config.hosts["h1"].ephemeral)
        self.assertEqual(config.hosts["cloudrun"].provider, "cloudrun")
        self.assertTrue(config.hosts["cloudrun"].ephemeral)
        self.assertEqual(config.hosts["cloudrun"].provider_config["project"],
                         "demo-project")


class ProvisionTests(Base):
    def test_queued_job_is_reserved_and_launched(self):
        job = self.submit()
        self.provisioner.tick()
        self.assertEqual(len(self.api.runs), 1)
        run = self.api.runs[0]
        self.assertEqual(run["job"], "ct-demo-light")
        self.assertEqual(run["env"]["CALLTABLE_JOB_ID"], job["id"])
        self.assertEqual(run["env"]["CALLTABLE_EXEC_HOST"], "cloudrun")
        self.assertEqual(run["env"]["CALLTABLE_HUB_URL"], "http://100.64.0.1:8091")
        self.assertEqual(run["timeout"], 20 * 60 + 300)
        reserved = self.store.job(job["id"])
        self.assertEqual(reserved["status"], "running")
        self.assertTrue(reserved["worker"].startswith(PENDING_WORKER_PREFIX))
        rows = self.store.cloud_executions(active_only=True)
        self.assertEqual(rows[0]["execution"], run["execution"])
        self.assertEqual((rows[0]["vcpu"], rows[0]["memory_gib"]), (2.0, 8.0))

    def test_launch_log_does_not_name_an_image(self):
        # 实际镜像由 Cloud Run 的 job 定义决定（试跑时是 :pre），hub 只能从
        # profile 推出 <registry>/<project>:latest，不能把它说成正在跑的镜像。
        job = self.submit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.provisioner.tick()
        execution = self.api.runs[0]["execution"]
        self.assertEqual(
            out.getvalue().splitlines(),
            [f"[cloudrun] {job['id']} -> ct-demo-light execution {execution}"])
        self.assertNotIn("image", out.getvalue())
        self.assertNotIn(":latest", out.getvalue())

    def test_heavy_rule_uses_the_heavy_job(self):
        self.submit(cmd=("bash", "gate.sh"))
        self.provisioner.tick()
        self.assertEqual(self.api.runs[0]["job"], "ct-demo-heavy")
        self.assertEqual(self.api.runs[0]["timeout"], 2 * 3600 + 300)

    def test_slots_cap_concurrent_executions(self):
        for _ in range(4):
            self.submit()
        self.provisioner.tick()
        self.assertEqual(len(self.api.runs), 2)  # light: 2
        self.provisioner.tick()
        self.assertEqual(len(self.api.runs), 2)

    def test_static_host_jobs_are_not_launched(self):
        self.store.heartbeat("h1", "w", [])
        self.submit(host="h1")
        self.provisioner.tick()
        self.assertEqual(self.api.runs, [])

    def test_launch_failure_fails_the_job(self):
        self.api.fail_run = True
        job = self.submit()
        self.provisioner.tick()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("quota exceeded", failed["error"])
        self.assertEqual(self.store.cloud_executions(active_only=True), [])

    def test_plain_claim_is_refused_on_provider_hosts(self):
        self.submit()
        self.store.heartbeat("cloudrun", "w", [])
        self.assertIsNone(self.hub.claim("cloudrun", "static-exec"))


class AttachTests(Base):
    def test_attach_binds_worker_once(self):
        job = self.submit()
        self.provisioner.tick()
        payload = self.hub.attach(job["id"], "cloudrun", "cloudrun-w1")
        self.assertEqual(payload["id"], job["id"])
        self.assertEqual(self.store.job(job["id"])["worker"], "cloudrun-w1")
        self.assertIsNotNone(self.store.cloud_executions()[0]["attached"])
        # 同一实例重试：幂等；另一个实例：拒绝。
        self.assertIsNotNone(self.hub.attach(job["id"], "cloudrun", "cloudrun-w1"))
        self.assertIsNone(self.hub.attach(job["id"], "cloudrun", "cloudrun-w2"))

    def test_attach_rejects_unreserved_or_foreign_jobs(self):
        job = self.submit()
        self.assertIsNone(self.hub.attach(job["id"], "cloudrun", "w"))  # queued
        self.provisioner.tick()
        self.assertIsNone(self.hub.attach(job["id"], "h1", "w"))
        self.assertIsNone(self.hub.attach("nope", "cloudrun", "w"))

    def test_heartbeats_of_parallel_executions_do_not_reap_each_other(self):
        first, second = self.submit(), self.submit()
        self.provisioner.tick()
        self.hub.attach(first["id"], "cloudrun", "w1")
        self.hub.attach(second["id"], "cloudrun", "w2")
        for _ in range(3):
            self.hub.heartbeat("cloudrun", "w1", [first["id"]])
            self.hub.heartbeat("cloudrun", "w2", [second["id"]])
        self.assertEqual(self.store.job(first["id"])["status"], "running")
        self.assertEqual(self.store.job(second["id"])["status"], "running")
        # 本实例丢了自己的任务，仍按 MISSING_STRIKES 回收。
        for _ in range(2):
            self.hub.heartbeat("cloudrun", "w1", [])
        self.assertEqual(self.store.job(first["id"])["status"], "error")
        self.assertEqual(self.store.job(second["id"])["status"], "running")

    def test_stale_host_reaper_skips_cloudrun_hosts(self):
        job = self.submit()
        self.provisioner.tick()
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.store.conn.execute("UPDATE hosts SET last_seen=1 WHERE name='cloudrun'")
        with mock.patch.object(self.provisioner, "tick"):
            self.hub.janitor_once()
        self.assertEqual(self.store.job(job["id"])["status"], "running")


class ReconcileTests(Base):
    def launched(self):
        job = self.submit()
        self.provisioner.tick()
        return job, self.api.runs[-1]["execution"]

    def test_execution_ending_before_the_report_fails_the_job(self):
        job, execution = self.launched()
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.api.complete(execution, failed=True)
        self.provisioner.tick()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("ended before the job reported", failed["error"])
        row = self.store.cloud_executions()[0]
        self.assertEqual(row["state"], "ended")
        self.assertIsNotNone(row["ended"])

    def test_finished_job_closes_its_execution_row(self):
        job, execution = self.launched()
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        self.api.complete(execution)
        self.provisioner.tick()
        self.assertEqual(self.store.job(job["id"])["status"], "succeeded")
        self.assertEqual(self.store.cloud_executions(active_only=True), [])
        self.assertEqual(self.api.cancelled, [])

    def test_lingering_execution_is_cancelled_after_the_grace(self):
        job, execution = self.launched()
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        self.store.conn.execute("UPDATE jobs SET finished=? WHERE id=?",
                                (now() - 3600, job["id"]))
        self.provisioner.tick()
        self.assertEqual(self.api.cancelled, [execution])

    def test_unattached_execution_times_out(self):
        job, execution = self.launched()
        self.store.conn.execute("UPDATE cloud_executions SET launched=?",
                                (now() - 3600,))
        self.provisioner.tick()
        self.assertEqual(self.api.cancelled, [execution])
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("never attached", failed["error"])

    def test_silent_executor_is_reaped(self):
        job, execution = self.launched()
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.store.conn.execute(
            "UPDATE cloud_executions SET attached=?, last_seen=?",
            (now() - 7200, now() - 7200))
        self.provisioner.tick()
        self.assertEqual(self.api.cancelled, [execution])
        self.assertIn("heartbeat lost", self.store.job(job["id"])["error"])


class JanitorIsolationTests(Base):
    """gcloud 变慢 / 卡住时不能拖住 janitor 的其他工作。"""

    api_class = BlockingAPI

    def tearDown(self):
        self.api.release.set()
        super().tearDown()

    def stale_static_job(self):
        self.store.heartbeat("h1", "w", [])
        job = self.submit(host="h1")
        self.assertEqual(self.store.claim("h1", "w")["id"], job["id"])
        self.store.conn.execute("UPDATE hosts SET last_seen=1 WHERE name='h1'")
        return job

    def stop_threads(self):
        self.api.release.set()
        self.hub.close()
        for thread in (self.hub._cloudrun_thread, self.hub._janitor):
            thread.join(20)
            self.assertFalse(thread.is_alive(), thread.name)

    def test_janitor_once_does_not_run_the_provisioner(self):
        job = self.stale_static_job()
        with mock.patch.object(self.provisioner, "tick") as tick, \
                contextlib.redirect_stdout(io.StringIO()):
            self.hub.janitor_once()
        tick.assert_not_called()
        self.assertEqual(self.store.job(job["id"])["status"], "error")

    def test_hung_gcloud_does_not_delay_reaping_static_hosts(self):
        self.config.raw["janitor_interval"] = 0.05
        cloud = self.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()  # 拉起；下一轮对账的 describe 会卡住
            static = self.stale_static_job()
            self.hub.start_janitor()
            self.assertTrue(self.api.entered.wait(20))
            wait_until(lambda: self.store.job(static["id"])["status"] == "error")
            # 回收发生时 provisioner 还卡在 gcloud 里。
            self.assertEqual(self.api.active, 1)
            self.assertFalse(self.api.release.is_set())
            self.assertEqual(self.store.job(cloud["id"])["status"], "running")
            self.stop_threads()

    def test_close_stops_the_provisioner_thread(self):
        self.config.raw["janitor_interval"] = 0.05
        self.submit()
        self.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
            self.hub.start_janitor()
            self.assertTrue(self.api.entered.wait(20))
            self.hub.close()
            self.stop_threads()
        # 关闭时正卡着的那次 describe 返回后，这一轮不再查剩下的 execution。
        self.assertEqual(self.api.describes, 1)
        self.assertEqual(self.api.cancelled, [])

    def test_ticks_never_overlap(self):
        self.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        first = threading.Thread(target=self.provisioner.tick)
        second = threading.Thread(target=self.hub.cloudrun_once)
        first.start()
        self.assertTrue(self.api.entered.wait(20))
        second.start()
        time.sleep(0.3)  # 没有单飞锁的话，第二轮此时已经进了 describe
        self.assertEqual(self.api.active, 1)
        self.api.release.set()
        first.join(20)
        second.join(20)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(self.api.max_active, 1)
        self.assertEqual(self.api.describes, 2)

    def test_closing_hub_stops_launching(self):
        # 关闭检查在 claim 之前：第二个任务留在排队里，而不是带着 pending:
        # 占位却没人拉起。
        for _ in range(2):
            self.submit()
        launch = self.api.run_job

        def launch_then_close(*args, **kwargs):
            name = launch(*args, **kwargs)
            self.hub.close()
            return name

        with mock.patch.object(self.api, "run_job", launch_then_close), \
                contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(len(self.api.runs), 1)
        self.assertEqual(sorted(job["status"] for job in self.store.jobs()),
                         ["queued", "running"])


class StaticHubThreadTests(unittest.TestCase):
    def test_hub_without_cloudrun_hosts_starts_no_provisioner_thread(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = make_config(Path(tmp), hosts={"h1": {"slots": {"light": 1}}})
            hub = Hub(config)
            try:
                hub.start_janitor()
                self.assertIsNone(hub.cloudrun)
                self.assertIsNone(hub._cloudrun_thread)
                hub.cloudrun_once()  # 没有 provider 主机时是空操作
            finally:
                hub.close()
                hub._janitor.join(20)
                hub.store.close()


class UnrecordedLaunchTests(Base):
    """hub 在 gcloud execute 期间 / claim 之后退出：execution 名或整条记录缺失。"""

    def reserve(self, *, record=True, launched=None):
        job = self.submit()
        self.store.touch_host("cloudrun")
        claimed = self.store.claim("cloudrun", f"{PENDING_WORKER_PREFIX}abc")
        self.assertEqual(claimed["id"], job["id"])
        if record:
            self.store.cloud_record_launch(
                job["id"], "cloudrun", cloud_job="ct-demo-light", vcpu=8,
                memory_gib=32, launched=launched if launched is not None else now())
        return job

    def restart(self):
        self.hub.close()
        self.api = FakeAPI()
        with contextlib.redirect_stdout(io.StringIO()):
            self.hub = Hub(self.config, cloudrun_api=self.api)
        self.store = self.hub.store
        self.provisioner = self.hub.cloudrun

    def test_crash_mid_launch_fails_the_job_and_stops_spend(self):
        job = self.reserve(launched=now() - 3600)
        self.restart()
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                self.hub.cloudrun_once()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("never recorded", failed["error"])
        self.assertEqual(self.store.cloud_executions(active_only=True), [])
        self.assertIsNotNone(self.store.cloud_executions()[0]["ended"])
        self.assertEqual(self.api.cancelled, [])  # 没有 execution 名，不调 API
        current = now()
        later = current + 600
        if cloudrun.month_start(later) != cloudrun.month_start(current - 3600):
            self.skipTest("month boundary")
        self.assertEqual(self.provisioner.month_spend("cloudrun", current),
                         self.provisioner.month_spend("cloudrun", later))

    def test_unrecorded_launch_waits_for_the_attach_timeout(self):
        job = self.reserve()
        self.provisioner.tick()
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.assertEqual(len(self.store.cloud_executions(active_only=True)), 1)

    def test_unrecorded_launch_with_lost_heartbeat_fails(self):
        job = self.reserve()
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.store.conn.execute(
            "UPDATE cloud_executions SET attached=?, last_seen=?",
            (now() - 7200, now() - 7200))
        self.provisioner.tick()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("heartbeat lost", failed["error"])
        self.assertEqual(self.store.cloud_executions(active_only=True), [])
        self.assertEqual(self.api.cancelled, [])

    def test_unrecorded_launch_that_attached_can_still_finish(self):
        # Cloud Run 其实拉起来了，只是名字没写回：容器 attach 并持续心跳就不能被回收。
        job = self.reserve(launched=now() - 3600)
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.hub.heartbeat("cloudrun", "w1", [job["id"]])
        self.provisioner.tick()
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.assertEqual(len(self.store.cloud_executions(active_only=True)), 1)
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        self.provisioner.tick()
        self.assertEqual(self.store.job(job["id"])["status"], "succeeded")
        self.assertEqual(self.store.cloud_executions(active_only=True), [])
        self.assertEqual(self.api.cancelled, [])

    def test_claim_without_a_row_fails_after_the_attach_timeout(self):
        job = self.reserve(record=False)
        self.provisioner.tick()
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.store.conn.execute("UPDATE jobs SET started=? WHERE id=?",
                                (now() - 3600, job["id"]))
        self.restart()
        with contextlib.redirect_stdout(io.StringIO()):
            self.hub.cloudrun_once()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("never recorded", failed["error"])


class OverflowGroupTests(Base):
    """溢出组 ``pool = [h1, cloudrun]``：还没 attach 就失败的组任务放回队列，
    之后冷却一段时间；h1 有空位就能领走。"""

    def setUp(self):
        super().setUp()
        self.config.raw["groups"] = {"pool": ["h1", "cloudrun"]}

    def fill_h1(self) -> list[dict]:
        """h1 在线但 light 槽位占满：组任务溢出到 cloudrun。"""
        self.store.heartbeat("h1", "w-h1", [])
        busy = []
        for _ in range(2):
            job = self.submit(host="h1")
            self.assertEqual(self.store.claim("h1", "w-h1")["id"], job["id"])
            busy.append(job)
        return busy

    def submit_group(self, **extra):
        return self.submit(host="pool", locks=["gate@{host}"], **extra)

    def tick(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.provisioner.tick()
        return out.getvalue()

    def assert_requeued(self, job: dict, reason: str) -> dict:
        current = self.store.job(job["id"])
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["host"], "pool")
        self.assertEqual(current["targets"], ["h1"])
        self.assertEqual(current["locks"], ["gate@{host}"])
        self.assertEqual(current["worker"], "")
        self.assertIsNone(current["started"])
        self.assertEqual(current["created"], job["created"])
        self.assertEqual(current["error"], "")
        self.assertIn(reason, current["requeue_note"])
        self.assertTrue(current["requeue_note"].startswith("cloudrun: "))
        types = [event["type"] for event in self.store.events_after(0, job["id"])]
        self.assertEqual(types[-1], "job.requeued")
        return current

    def assert_claimed_by_h1(self, job: dict, busy: list[dict]) -> None:
        self.store.finish(busy[0]["id"], status="succeeded", exit_code=0)
        claimed = self.store.claim("h1", "w-h1")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["host"], "h1")
        self.assertEqual(claimed["locks"], ["gate@h1"])

    def test_group_job_overflows_to_cloudrun_when_h1_is_full(self):
        self.fill_h1()
        job = self.submit_group()
        self.assertEqual(job["targets"], ["h1", "cloudrun"])
        self.tick()
        reserved = self.store.job(job["id"])
        self.assertEqual(reserved["host"], "cloudrun")
        self.assertEqual(reserved["locks"], ["gate@cloudrun"])
        self.assertEqual(len(self.api.runs), 1)

    def test_project_without_a_cloudrun_profile_never_overflows(self):
        self.fill_h1()
        job = self.hub.create_job({"project": "other", "host": "pool",
                                   "cmd": ["true"], "client_version": "9.9.9"})
        self.assertEqual(job["targets"], ["h1"])
        self.tick()
        self.assertEqual(self.api.runs, [])
        self.assertEqual(self.store.job(job["id"])["status"], "queued")

    def test_launch_failure_requeues_the_group_job(self):
        busy = self.fill_h1()
        self.api.fail_run = True
        job = self.submit_group()
        out = self.tick()
        self.assert_requeued(job, "quota exceeded")
        self.assertIn("back in the queue for h1", out)
        row = self.store.cloud_executions()[0]
        self.assertEqual(row["state"], "failed")
        self.assertIsNotNone(row["ended"])
        self.assertIn("quota exceeded", row["note"])
        self.assert_claimed_by_h1(job, busy)

    def test_attach_timeout_requeues_and_cancels_the_execution(self):
        busy = self.fill_h1()
        job = self.submit_group()
        self.tick()
        execution = self.api.runs[0]["execution"]
        self.store.conn.execute("UPDATE cloud_executions SET launched=?",
                                (now() - 3600,))
        self.tick()
        self.assertEqual(self.api.cancelled, [execution])
        self.assert_requeued(job, "never attached")
        row = self.store.cloud_executions()[0]
        self.assertEqual(row["state"], "cancelled")
        # 放回队列之后，迟到的容器 attach 不上。
        self.assertIsNone(self.hub.attach(job["id"], "cloudrun", "late"))
        self.assert_claimed_by_h1(job, busy)

    def test_execution_ending_without_attach_requeues(self):
        busy = self.fill_h1()
        job = self.submit_group()
        self.tick()
        self.api.complete(self.api.runs[0]["execution"], failed=True)
        self.tick()
        self.assert_requeued(job, "ended before the job reported")
        self.assertEqual(self.store.cloud_executions()[0]["state"], "ended")
        self.assert_claimed_by_h1(job, busy)

    def test_execution_ending_after_attach_still_fails(self):
        self.fill_h1()
        job = self.submit_group()
        self.tick()
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.api.complete(self.api.runs[0]["execution"], failed=True)
        self.tick()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("ended before the job reported", failed["error"])

    def test_pending_placeholder_timeout_requeues(self):
        busy = self.fill_h1()
        job = self.submit_group()
        self.store.touch_host("cloudrun")
        self.assertEqual(self.store.claim("cloudrun", f"{PENDING_WORKER_PREFIX}abc")
                         ["id"], job["id"])
        self.store.conn.execute("UPDATE jobs SET started=? WHERE id=?",
                                (now() - 3600, job["id"]))
        self.tick()
        self.assert_requeued(job, "launch was never recorded")
        self.assert_claimed_by_h1(job, busy)

    def test_unrecorded_execution_name_timeout_requeues(self):
        busy = self.fill_h1()
        job = self.submit_group()
        self.store.touch_host("cloudrun")
        self.store.claim("cloudrun", f"{PENDING_WORKER_PREFIX}abc")
        self.store.cloud_record_launch(job["id"], "cloudrun", cloud_job="ct-demo-light",
                                       vcpu=2, memory_gib=8, launched=now() - 3600)
        self.tick()
        self.assert_requeued(job, "execution name was never recorded")
        self.assertEqual(self.store.cloud_executions()[0]["state"], "failed")
        self.assert_claimed_by_h1(job, busy)

    def test_single_target_job_still_errors(self):
        self.api.fail_run = True
        job = self.submit(host="cloudrun")
        self.tick()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["requeue_note"], "")

    def test_attach_racing_the_timeout_leaves_the_job_alone(self):
        self.fill_h1()
        job = self.submit_group()
        self.tick()
        self.hub.attach(job["id"], "cloudrun", "w1")
        # provisioner 读到的是 attach 之前的 execution 快照。
        self.store.conn.execute(
            "UPDATE cloud_executions SET attached=NULL, launched=?", (now() - 3600,))
        self.tick()
        current = self.store.job(job["id"])
        self.assertEqual((current["status"], current["host"], current["worker"]),
                         ("running", "cloudrun", "w1"))
        self.assertEqual(self.api.cancelled, [])
        self.assertFalse(self.provisioner.summary()["cloudrun"]["cooling_down"])

    def test_cooldown_pauses_reservations_without_starving_group_jobs(self):
        busy = self.fill_h1()
        self.api.fail_run = True
        first = self.submit_group()
        out = self.tick()
        self.assertEqual(out.count("not reserving new jobs for 300s"), 1)
        self.api.fail_run = False
        second = self.submit(host="pool")  # 不加锁：不和 first 抢 gate@h1
        direct = self.submit(host="cloudrun")
        self.tick()
        self.assertEqual(self.api.runs, [])  # 冷却中：不预留、不拉起
        summary = self.provisioner.summary()["cloudrun"]
        self.assertTrue(summary["cooling_down"])
        self.assertEqual(summary["launch_cooldown"], 300)
        self.assertIn("quota exceeded", summary["cooldown_reason"])
        self.assertGreater(summary["cooldown_until"], now())
        # cloudrun 在线、有空位，但冷却中不能挡住 h1：h1 一有空位就领走。
        self.store.touch_host("cloudrun")
        for job, done in ((first, busy[0]), (second, busy[1])):
            self.store.finish(done["id"], status="succeeded", exit_code=0)
            self.assertEqual(self.store.claim("h1", "w-h1")["id"], job["id"])
        self.assertEqual(self.store.job(direct["id"])["status"], "queued")
        # 冷却结束：直接发给 cloudrun 的任务照常拉起。
        self.provisioner._cooldowns["cloudrun"] = (now() - 1, "")
        self.store.set_host_cooldown("cloudrun", now() - 1)
        self.tick()
        self.assertEqual(len(self.api.runs), 1)
        self.assertEqual(self.api.runs[0]["env"]["CALLTABLE_JOB_ID"], direct["id"])
        self.assertFalse(self.provisioner.summary()["cloudrun"]["cooling_down"])

    def test_repeated_failures_log_the_cooldown_once(self):
        settings = self.provisioner.settings["cloudrun"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.provisioner._start_cooldown("cloudrun", settings, "first")
            self.provisioner._start_cooldown("cloudrun", settings, "second")
        self.assertEqual(out.getvalue().count("not reserving new jobs"), 1)
        self.assertEqual(self.provisioner.summary()["cloudrun"]["cooldown_reason"],
                         "second")


class NoCooldownTests(Base):
    hosts_overrides = {"launch_cooldown": "0"}

    def test_zero_disables_the_cooldown(self):
        self.api.fail_run = True
        self.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.api.fail_run = False
        self.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(len(self.api.runs), 1)
        self.assertFalse(self.provisioner.summary()["cloudrun"]["cooling_down"])
        self.assertEqual(self.store.host_cooldowns, {})


class StaleSnapshotTests(Base):
    """慢速 gcloud 调用期间执行器上报了成功：provisioner 不能把它改成 error。"""

    def attached(self):
        job = self.submit()
        self.provisioner.tick()
        execution = self.api.runs[-1]["execution"]
        self.hub.attach(job["id"], "cloudrun", "w1")
        return job, execution

    def test_success_during_describe_is_kept(self):
        job, execution = self.attached()
        original = self.api.describe_execution

        def slow_describe(settings, name):
            self.store.finish(job["id"], status="succeeded", exit_code=0)
            self.api.complete(name)
            return original(settings, name)

        self.api.describe_execution = slow_describe
        self.provisioner.tick()
        self.assertEqual(self.store.job(job["id"])["status"], "succeeded")
        self.assertEqual(self.store.cloud_executions(active_only=True), [])

    def test_success_during_cancel_is_kept(self):
        job, execution = self.attached()
        self.store.conn.execute(
            "UPDATE cloud_executions SET attached=?, last_seen=?",
            (now() - 7200, now() - 7200))

        def slow_cancel(settings, name):
            self.store.finish(job["id"], status="succeeded", exit_code=0)
            self.api.cancelled.append(name)

        self.api.cancel_execution = slow_cancel
        self.provisioner.tick()
        self.assertEqual(self.api.cancelled, [execution])
        self.assertEqual(self.store.job(job["id"])["status"], "succeeded")

    def test_finish_if_status_leaves_terminal_jobs_alone(self):
        job = self.submit()
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        self.assertIsNone(self.store.finish_if_status(
            job["id"], ("running",), status="error", exit_code=None, error="x"))
        self.assertEqual(self.store.job(job["id"])["status"], "succeeded")
        self.assertIsNone(self.store.finish_if_status(
            "nope", ("running",), status="error", exit_code=None))


class UnknownProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        hosts = {"odd": {"slots": {"light": 1, "heavy": 1},
                         "provider": "cloud-run"}}
        self.config = make_config(Path(self.tmp.name), hosts=hosts)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.hub = Hub(self.config)
        self.addCleanup(self.hub.close)
        self.warnings = out.getvalue()

    def test_unknown_provider_is_a_static_host(self):
        self.assertIn("unknown provider 'cloud-run'", self.warnings)
        self.assertFalse(self.config.hosts["odd"].ephemeral)
        self.assertIsNone(self.hub.cloudrun)
        self.assertNotIn("odd", self.hub.ephemeral_hosts())
        job = self.hub.create_job({"project": "demo", "host": "odd",
                                   "cmd": ["echo", "hi"],
                                   "client_version": "9.9.9"})
        self.hub.heartbeat("odd", "w1", [])
        payload = self.hub.claim("odd", "w1")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["id"], job["id"])
        # 普通心跳对账：换了 worker 立即回收（ephemeral 主机会跳过）。
        self.hub.heartbeat("odd", "w2", [])
        self.assertEqual(self.hub.store.job(job["id"])["status"], "error")

    def test_unknown_provider_host_is_reaped_when_stale(self):
        job = self.hub.create_job({"project": "demo", "host": "odd",
                                   "cmd": ["echo", "hi"],
                                   "client_version": "9.9.9"})
        self.hub.heartbeat("odd", "w1", [])
        self.assertIsNotNone(self.hub.claim("odd", "w1"))
        self.hub.store.conn.execute("UPDATE hosts SET last_seen=1 WHERE name='odd'")
        with contextlib.redirect_stdout(io.StringIO()):
            self.hub.janitor_once()
        self.assertEqual(self.hub.store.job(job["id"])["status"], "error")


class BudgetTests(Base):
    hosts_overrides = {"monthly_budget_usd": 1.0}

    def test_month_spend_counts_running_and_finished_executions(self):
        current = now()
        self.store.cloud_record_launch("a", "cloudrun", cloud_job="j", vcpu=8,
                                       memory_gib=32, launched=current - 3600)
        self.store.cloud_update("a", ended=current - 1800)
        self.store.cloud_record_launch("b", "cloudrun", cloud_job="j", vcpu=2,
                                       memory_gib=8, launched=current - 600)
        settings = self.provisioner.settings["cloudrun"]
        expected = settings.cost(1800, 8, 32) + settings.cost(600, 2, 8)
        start = cloudrun.month_start(current)
        if current - 3600 < start:  # 测试恰好跨月：只算本月部分
            self.skipTest("month boundary")
        self.assertAlmostEqual(self.provisioner.month_spend("cloudrun", current),
                               expected, places=2)

    def test_previous_month_is_not_counted(self):
        start = cloudrun.month_start(now())
        self.store.cloud_record_launch("old", "cloudrun", cloud_job="j", vcpu=8,
                                       memory_gib=32, launched=start - 7200)
        self.store.cloud_update("old", ended=start - 3600)
        self.assertEqual(self.provisioner.month_spend("cloudrun"), 0)

    def test_over_budget_keeps_jobs_queued(self):
        self.store.cloud_record_launch("old", "cloudrun", cloud_job="j", vcpu=8,
                                       memory_gib=32, launched=now() - 7200)
        self.store.cloud_update("old", ended=now() - 1)
        job = self.submit()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.provisioner.tick()
        self.assertEqual(self.api.runs, [])
        self.assertEqual(self.store.job(job["id"])["status"], "queued")
        self.assertIn("monthly cap", out.getvalue())
        summary = self.provisioner.summary()["cloudrun"]
        self.assertTrue(summary["over_budget"])


class BudgetFailTests(Base):
    hosts_overrides = {"monthly_budget_usd": 0.0001, "on_budget_exceeded": "fail"}

    def test_over_budget_can_fail_jobs_instead(self):
        self.store.cloud_record_launch("old", "cloudrun", cloud_job="j", vcpu=1,
                                       memory_gib=1, launched=now() - 120)
        self.store.cloud_update("old", ended=now() - 1)
        job = self.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        failed = self.store.job(job["id"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("monthly cap", failed["error"])


class GcloudAPITests(unittest.TestCase):
    def setUp(self):
        self.settings = CloudRunSettings.from_host("cloudrun", {
            "project": "p", "region": "asia-east1", "gcloud": "/opt/gcloud"})
        self.api = cloudrun.GcloudCloudRunAPI()

    def fake_run(self, stdout="", stderr="", code=0):
        result = mock.Mock(returncode=code, stdout=stdout, stderr=stderr)
        return mock.patch.object(cloudrun.subprocess, "run", return_value=result)

    def test_execute_command_and_name_from_json(self):
        body = json.dumps({"metadata": {"name": "ct-demo-light-abcde"}})
        with self.fake_run(stdout=body) as run:
            name = self.api.run_job(self.settings, "ct-demo-light",
                                    env={"CALLTABLE_JOB_ID": "job-1"},
                                    task_timeout=900)
        self.assertEqual(name, "ct-demo-light-abcde")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:5], ["/opt/gcloud", "run", "jobs", "execute",
                                    "ct-demo-light"])
        self.assertIn("--async", argv)
        self.assertIn("--region=asia-east1", argv)
        self.assertIn("--task-timeout=900s", argv)
        self.assertIn("--update-env-vars=CALLTABLE_JOB_ID=job-1", argv)

    def test_execute_name_from_stderr_message(self):
        with self.fake_run(stderr="Execution [ct-demo-light-x7k2p] has "
                                  "successfully started running."):
            name = self.api.run_job(self.settings, "ct-demo-light", env={},
                                    task_timeout=60)
        self.assertEqual(name, "ct-demo-light-x7k2p")

    def test_errors_are_raised(self):
        with self.fake_run(stderr="PERMISSION_DENIED", code=1):
            with self.assertRaises(cloudrun.CloudRunError):
                self.api.cancel_execution(self.settings, "x")

    def test_execution_finished_parsing(self):
        self.assertEqual(cloudrun.execution_finished({"status": {}}), (False, ""))
        done, detail = cloudrun.execution_finished({"status": {
            "completionTime": "2026-11-25T00:00:00Z", "succeededCount": 1}})
        self.assertTrue(done)
        self.assertIn("succeeded=1", detail)
        self.assertIsNotNone(cloudrun.parse_rfc3339("2026-11-25T06:00:00.123456789Z"))


class CliTests(Base):
    def run_cli(self, *argv):
        from calltable.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["cli", "cloudrun", *argv, "--config", str(self.config.path)])
        return code, out.getvalue()

    def test_jobs_dry_run_prints_both_slots_with_max_retries_zero(self):
        with mock.patch.object(cloudrun_cli.subprocess, "call") as call:
            code, out = self.run_cli("jobs", "--project", "demo")
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertIn("gcloud run jobs deploy ct-demo-light", out)
        self.assertIn("gcloud run jobs deploy ct-demo-heavy", out)
        self.assertEqual(out.count("--max-retries=0"), 2)
        self.assertIn("--cpu=8 --memory=32Gi", out)
        self.assertIn("CALLTABLE_TOKEN=calltable-hub-token:latest", out)
        self.assertIn("TS_AUTHKEY=calltable-tailscale-authkey:latest", out)
        self.assertIn("--service-account=calltable-exec@demo-project", out)
        self.assertIn("us-central1-docker.pkg.dev/demo-project/calltable/demo:latest",
                      out)
        self.assertIn("dry run", out)

    def test_image_dry_run_writes_a_build_context(self):
        context = self.root / "ctx"
        with mock.patch.object(cloudrun_cli.subprocess, "call") as call:
            code, out = self.run_cli("image", "--project", "demo",
                                     "--context", str(context), "--tag", "v1")
        self.assertEqual(code, 0)
        call.assert_not_called()
        dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG BASE_IMAGE=mcr.microsoft.com/dotnet/sdk:10.0", dockerfile)
        self.assertIn("RUN apt-get update && apt-get install -y jq", dockerfile)
        self.assertIn("ENV DOTNET_NOLOGO=1", dockerfile)
        self.assertIn("tailscale/tailscale", dockerfile)
        self.assertTrue((context / "entrypoint.sh").exists())
        self.assertTrue((context / "calltable" / "executor.py").exists())
        self.assertIn("gcloud builds submit", out)
        self.assertIn("demo-project/calltable/demo:v1", out)

    def test_apply_runs_the_commands(self):
        with mock.patch.object(cloudrun_cli.subprocess, "call",
                               return_value=0) as call:
            code, _ = self.run_cli("jobs", "--project", "demo", "--slots",
                                   "light", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args[0][0][:5],
                         ["gcloud", "run", "jobs", "deploy", "ct-demo-light"])

    def test_bootstrap_prints_iam_for_the_hub_identity(self):
        code, out = self.run_cli("bootstrap", "--hub-account",
                                 "calltable-hub@demo-project.iam.gserviceaccount.com")
        self.assertEqual(code, 0)
        self.assertIn("gcloud artifacts repositories create calltable", out)
        self.assertIn("roles/secretmanager.secretAccessor", out)
        self.assertIn("roles/run.developer", out)
        self.assertIn("roles/iam.serviceAccountUser", out)

    def test_unknown_slot_is_rejected(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cli("jobs", "--project", "demo", "--slots", "gpu")
        self.assertEqual(code, 2)


class ProxyTests(unittest.TestCase):
    def test_hub_proxy_only_affects_the_hub_client(self):
        client = HubClient("http://100.64.0.1:8091", "t",
                           proxy="http://localhost:1055")
        handlers = [h for h in client._opener.handlers
                    if h.__class__.__name__ == "ProxyHandler"]
        self.assertEqual(handlers[0].proxies["http"], "http://localhost:1055")
        self.assertIsNone(HubClient("http://x", "t")._opener)


class _Recorder(http.server.BaseHTTPRequestHandler):
    """假 hub：记录收到的上传。"""
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.received.append({"path": self.path, "body": body,
                              "auth": self.headers.get("Authorization"),
                              "x": self.headers.get("X-Calltable-Path")})
        payload = json.dumps({"ok": True, "size": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class _ForwardProxy(http.server.BaseHTTPRequestHandler):
    """最小的正向代理：只接受绝对 URI 请求目标，原样转发到目标主机。"""
    targets: list = []

    def do_POST(self):
        self.targets.append(self.path)
        target = urllib.parse.urlparse(self.path)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        upstream = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "connection", "proxy-connection")}
        upstream.request("POST", target.path, body=body, headers=headers)
        response = upstream.getresponse()
        payload = response.read()
        upstream.close()
        self.send_response(response.status)
        self.send_header("Content-Type", response.getheader("Content-Type", ""))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class UploadProxyTests(unittest.TestCase):
    def serve(self, handler):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def setUp(self):
        _Recorder.received = []
        _ForwardProxy.targets = []
        self.hub_url = self.serve(_Recorder)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.data = bytes(range(256)) * 9000  # 多块（block 默认 1 MiB）
        self.file = Path(tmp.name) / "a.bin"
        self.file.write_bytes(self.data)

    def check_received(self, result):
        self.assertEqual(result, {"ok": True, "size": len(self.data)})
        self.assertEqual(len(_Recorder.received), 1)
        got = _Recorder.received[0]
        self.assertEqual(got["path"], "/api/jobs/j1/artifacts")
        self.assertEqual(got["body"], self.data)
        self.assertEqual(got["auth"], "Bearer t")
        self.assertEqual(got["x"], "out%2Fa.bin")

    def test_upload_goes_through_the_hub_proxy(self):
        proxy = self.serve(_ForwardProxy)
        client = HubClient(self.hub_url, "t", proxy=proxy)
        result = client.upload_file("/api/jobs/j1/artifacts", self.file,
                                    headers={"X-Calltable-Path": "out%2Fa.bin"})
        self.check_received(result)
        self.assertEqual(_ForwardProxy.targets,
                         [self.hub_url + "/api/jobs/j1/artifacts"])

    def test_upload_without_proxy_goes_direct(self):
        client = HubClient(self.hub_url, "t")
        result = client.upload_file("/api/jobs/j1/artifacts", self.file,
                                    headers={"X-Calltable-Path": "out%2Fa.bin"},
                                    block=4096)
        self.check_received(result)
        self.assertEqual(_ForwardProxy.targets, [])

    def test_https_hub_uses_a_connect_tunnel(self):
        client = HubClient("https://hub.example.ts.net", "t",
                           proxy="http://localhost:1055")
        with mock.patch.object(http.client, "HTTPSConnection") as conn_cls:
            conn = conn_cls.return_value
            conn.getresponse.return_value = mock.Mock(status=200,
                                                      read=lambda: b"")
            client.upload_file("/api/jobs/j1/artifacts", self.file)
        self.assertEqual(conn_cls.call_args[0][:2], ("localhost", 1055))
        conn.set_tunnel.assert_called_once_with("hub.example.ts.net", 443)
        conn.putrequest.assert_called_once_with("POST", "/api/jobs/j1/artifacts")
        sent = b"".join(call.args[0] for call in conn.send.call_args_list)
        self.assertEqual(sent, self.data)


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def run_git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class GitRelaySettingsTests(unittest.TestCase):
    def test_feature_is_off_without_a_url(self):
        self.assertIsNone(cloudrun.GitRelay.from_profile({}))
        self.assertIsNone(cloudrun.GitRelay.from_profile(None))
        self.assertIsNone(cloudrun.GitRelay.from_profile({"git": {"branch": "x"}}))

    def test_defaults(self):
        relay = cloudrun.GitRelay.from_profile(
            {"git": {"url": "https://github.com/o/r.git"}})
        self.assertEqual(relay.push_url, "https://github.com/o/r.git")
        self.assertEqual(relay.branch, "main")
        self.assertEqual(relay.token_secret, "")

    def test_unsafe_values_are_rejected(self):
        bad = [
            {"url": "https://github.com/o/r.git x"},
            {"url": "https://github.com/o/r.git\nimages: [evil]"},
            {"url": "https://github.com/o/r.git;rm"},
            {"url": "https://github.com/o/'r'.git"},
            {"url": "--upload-pack=evil"},
            {"url": "https://github.com/o/r.git,X=1"},
            {"url": "https://user:secret@github.com/o/r.git"},
            {"url": "https://github.com/o/r.git", "push_url": "$(evil)"},
            {"url": "https://github.com/o/r.git", "branch": "a b"},
            {"url": "https://github.com/o/r.git", "branch": "a..b"},
            {"url": "https://github.com/o/r.git", "branch": "-x"},
            {"url": "https://github.com/o/r.git", "token_secret": "a:b"},
            {"url": "https://github.com/o/r.git", "token_secret": "a\nb"},
        ]
        for git_settings in bad:
            with self.subTest(git_settings=git_settings):
                with self.assertRaises(ValueError):
                    cloudrun.GitRelay.from_profile({"git": git_settings})
        cloudrun.GitRelay.from_profile({"git": {
            "url": "https://github.com/o/r.git",
            "push_url": "git@github.com:o/r.git",
            "branch": "release/1.2", "token_secret": "calltable-git_x"}})
        with self.assertRaises(ValueError):
            CloudRunSettings.from_host("c", {"project": "p",
                                             "git_token_secret": "bad name"})

    def test_token_secret_resolution(self):
        url = "https://github.com/o/r.git"
        resolve = cloudrun.GitRelay.from_profile
        # profile 的值优先
        self.assertEqual(resolve({"git": {"url": url, "token_secret": "mine"}},
                                 "shared").token_secret, "mine")
        # 没有这个键：用主机默认
        self.assertEqual(resolve({"git": {"url": url}}, "shared").token_secret,
                         "shared")
        # 显式空串 / null：不用 token
        self.assertEqual(resolve({"git": {"url": url, "token_secret": ""}},
                                 "shared").token_secret, "")
        self.assertEqual(resolve({"git": {"url": url, "token_secret": None}},
                                 "shared").token_secret, "")
        settings = CloudRunSettings.from_host(
            "c", {"project": "p", "git_token_secret": "shared"})
        self.assertEqual(settings.git_token_secret, "shared")
        self.assertEqual(CloudRunSettings.from_host("c", {"project": "p"})
                         .git_token_secret, "")


class GitRelayBase(Base):
    """hub 镜像里有两个提交；「GitHub」是本地裸仓库。"""

    def profile_git(self):
        self.remote = Path(self.tmp.name) / "remote.git"
        return {"url": str(self.remote), "push_url": str(self.remote)}

    def setUp(self):
        super().setUp()
        run_git(self.root, "init", "-q", "--bare", str(self.remote))
        src = self.root / "src"
        src.mkdir()
        run_git(src, "init", "-q", "-b", "main")
        (src / "a.txt").write_text("one\n", encoding="utf-8")
        run_git(src, "add", "-A")
        run_git(src, "commit", "-qm", "one")
        self.base_sha = run_git(src, "rev-parse", "HEAD")
        (src / "a.txt").write_text("two\n", encoding="utf-8")
        run_git(src, "commit", "-qam", "two")
        self.sha = run_git(src, "rev-parse", "HEAD")
        mirror = self.hub.mirror_for("demo")
        run_git(mirror, "fetch", "-q", str(src), "+refs/heads/*:refs/heads/*")

    def submit_git(self):
        return self.submit(sha=self.sha, base_ref="origin/main",
                           base_ref_sha=self.base_sha)

    def remote_refs(self) -> dict:
        out = run_git(self.root, "ls-remote", str(self.remote))
        return {line.split()[1]: line.split()[0] for line in out.splitlines()}

    def launch(self):
        job = self.submit_git()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.provisioner.tick()
        return job, out.getvalue()

    def close(self, job):
        execution = self.api.runs[-1]["execution"]
        self.hub.attach(job["id"], "cloudrun", "w1")
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        self.api.complete(execution)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.provisioner.tick()
        return out.getvalue()


class GitRelayHubTests(GitRelayBase):
    def test_hub_pushes_both_refs_and_passes_the_variables(self):
        job, out = self.launch()
        prefix = f"refs/calltable/jobs/{job['id']}"
        env = self.api.runs[0]["env"]
        self.assertEqual(env["CALLTABLE_GIT_URL"], str(self.remote))
        self.assertEqual(env["CALLTABLE_GIT_REFS"], prefix)
        self.assertEqual(self.remote_refs(), {f"{prefix}/head": self.sha,
                                              f"{prefix}/base": self.base_sha})
        row = self.store.cloud_executions()[0]
        self.assertEqual(json.loads(row["git_refs"])["refs"],
                         [f"{prefix}/base", f"{prefix}/head"])
        self.assertNotIn("failed", out)

    def test_push_failure_falls_back_and_still_launches(self):
        shutil.rmtree(self.remote)
        job, out = self.launch()
        self.assertEqual(len(self.api.runs), 1)
        env = self.api.runs[0]["env"]
        self.assertNotIn("CALLTABLE_GIT_URL", env)
        self.assertNotIn("CALLTABLE_GIT_REFS", env)
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        warnings = [line for line in out.splitlines() if "git push" in line]
        self.assertEqual(len(warnings), 1, out)
        self.assertTrue(warnings[0].startswith(f"[cloudrun] {job['id']}: "))
        self.assertIn("falling back to hub bundles", warnings[0])
        # 计划推的 ref 先记下了：清理流程会处理（一个都没落地就算完成）。
        prefix = f"refs/calltable/jobs/{job['id']}"
        self.assertEqual(json.loads(self.store.cloud_executions()[0]["git_refs"])
                         ["refs"], [f"{prefix}/base", f"{prefix}/head"])

    def test_missing_base_pushes_head_only(self):
        job = self.submit(sha=self.sha, base_ref="origin/main",
                          base_ref_sha="f" * 40)
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(list(self.remote_refs()),
                         [f"refs/calltable/jobs/{job['id']}/head"])
        self.assertIn("CALLTABLE_GIT_REFS", self.api.runs[0]["env"])

    def test_refs_are_deleted_after_the_row_closes(self):
        job, _ = self.launch()
        # execution 还在跑：ref 不能删
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(len(self.remote_refs()), 2)
        self.close(job)
        self.assertEqual(self.store.cloud_executions(active_only=True), [])
        self.assertEqual(self.remote_refs(), {})
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")

    def test_failed_deletion_is_retried_later(self):
        job, _ = self.launch()
        moved = self.root / "remote-away.git"
        self.remote.rename(moved)
        out = self.close(job)
        self.assertIn("deleting git refs", out)
        self.assertIn("will retry", out)
        self.assertNotEqual(self.store.cloud_executions()[0]["git_refs"], "")
        moved.rename(self.remote)
        # 退避期内不重试
        with mock.patch.object(cloudrun, "run_git") as git_call:
            with contextlib.redirect_stdout(io.StringIO()):
                self.provisioner.tick()
        git_call.assert_not_called()
        self.assertEqual(len(self.remote_refs()), 2)
        self.provisioner._git_retry_at.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(self.remote_refs(), {})
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")

    def test_partially_deleted_refs_count_as_cleaned(self):
        job, _ = self.launch()
        run_git(self.root, "--git-dir", str(self.remote), "update-ref", "-d",
                f"refs/calltable/jobs/{job['id']}/base")
        out = self.close(job)
        self.assertEqual(self.remote_refs(), {})
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")
        self.assertNotIn("will retry", out)

    def test_launch_failure_still_cleans_up_the_refs(self):
        self.api.fail_run = True
        job, _ = self.launch()
        self.assertEqual(self.store.job(job["id"])["status"], "error")
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(self.remote_refs(), {})

    def test_requeued_group_job_still_cleans_up_the_refs(self):
        self.config.raw["groups"] = {"pool": ["h1", "cloudrun"]}
        self.api.fail_run = True
        job = self.submit(host="pool", sha=self.sha, base_ref="origin/main",
                          base_ref_sha=self.base_sha)
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(self.store.job(job["id"])["status"], "queued")
        self.assertEqual(len(self.remote_refs()), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.provisioner.tick()
        self.assertEqual(self.remote_refs(), {})

    def test_closing_hub_skips_the_push(self):
        job = self.submit_git()
        with mock.patch.object(self.provisioner, "_closing", return_value=True):
            env = self.provisioner._push_git_refs(self.store.job(job["id"]))
        self.assertEqual(env, {})
        self.assertEqual(self.remote_refs(), {})


class GitRelayVisibilityTests(GitRelayBase):
    """推送前匿名 ls-remote：公开 / 无法确认都不推；私有照推；结论按 URL 缓存。"""

    URL = "https://git.example.invalid/o/demo.git"
    PUSH_URL = "git@git.example.invalid:o/demo.git"

    def profile_git(self):
        self.remote = Path(self.tmp.name) / "remote.git"
        return {"url": self.URL, "push_url": self.PUSH_URL}

    def setUp(self):
        super().setUp()
        # push_url 必须和 url 是同一个仓库；测试里把对它的 git 调用改投到本地
        # 裸仓库（不联网）。
        real_run_git = cloudrun.run_git

        def redirect(args, **kwargs):
            args = [str(self.remote) if arg == self.PUSH_URL else arg
                    for arg in args]
            return real_run_git(args, **kwargs)

        patch = mock.patch.object(cloudrun, "run_git", side_effect=redirect)
        patch.start()
        self.addCleanup(patch.stop)

    def fake_ls_remote(self, code=0, stderr="", exc=None):
        calls = []

        def run(url, timeout=cloudrun.VISIBILITY_TIMEOUT):
            calls.append((url, timeout))
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(["git"], code, "", stderr)

        return calls, mock.patch.object(cloudrun, "anonymous_ls_remote",
                                        side_effect=run)

    def test_public_repository_is_never_pushed_to(self):
        calls, patch = self.fake_ls_remote(code=0)
        with patch, mock.patch.object(cloudrun, "run_git") as git_call:
            job, out = self.launch()
        self.assertEqual(calls, [(self.URL, cloudrun.VISIBILITY_TIMEOUT)])
        git_call.assert_not_called()
        env = self.api.runs[0]["env"]
        self.assertNotIn("CALLTABLE_GIT_URL", env)
        self.assertNotIn("CALLTABLE_GIT_REFS", env)
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")
        self.assertEqual(self.remote_refs(), {})
        warnings = [line for line in out.splitlines() if "publicly readable" in line]
        self.assertEqual(len(warnings), 1, out)
        self.assertTrue(warnings[0].startswith(f"[cloudrun] {job['id']}: refusing"))

    def test_http_403_is_not_proof_of_a_private_repository(self):
        """挡在公开仓库前面的代理 / 防火墙也会回 403：按无法确认处理，不推送。"""
        _, patch = self.fake_ls_remote(
            128, "fatal: unable to access '...': The requested URL "
                 "returned error: 403")
        with patch:
            verdict, _ = cloudrun.repo_visibility(self.URL)
        self.assertEqual(verdict, cloudrun.UNKNOWN)

    def test_private_repository_is_pushed_as_before(self):
        for stderr in ("fatal: could not read Username for 'https://git.example."
                       "invalid': terminal prompts disabled",
                       "remote: Repository not found.\nfatal: repository "
                       "'https://git.example.invalid/o/demo.git/' not found",
                       "fatal: unable to access '...': The requested URL "
                       "returned error: 401",
                       "fatal: Authentication failed for 'https://x/'"):
            with self.subTest(stderr=stderr):
                _, patch = self.fake_ls_remote(128, stderr)
                with patch:
                    verdict, _ = cloudrun.repo_visibility(self.URL)
                self.assertEqual(verdict, cloudrun.PRIVATE)
        calls, patch = self.fake_ls_remote(
            code=128, stderr="fatal: could not read Username: terminal prompts "
                             "disabled")
        with patch:
            job, out = self.launch()
        self.assertEqual(len(calls), 1)
        prefix = f"refs/calltable/jobs/{job['id']}"
        self.assertEqual(self.api.runs[0]["env"]["CALLTABLE_GIT_URL"], self.URL)
        self.assertEqual(self.api.runs[0]["env"]["CALLTABLE_GIT_REFS"], prefix)
        self.assertEqual(set(self.remote_refs()), {f"{prefix}/head", f"{prefix}/base"})
        self.assertNotEqual(self.store.cloud_executions()[0]["git_refs"], "")
        self.assertNotIn("publicly", out)

    def test_unknown_visibility_fails_closed(self):
        for kwargs in ({"exc": subprocess.TimeoutExpired(["git"], 20)},
                       {"exc": OSError("no git")},
                       {"code": 128, "stderr": "fatal: unable to access: Could "
                                               "not resolve host: git.example.invalid"}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                self.provisioner._visibility.clear()
                calls, patch = self.fake_ls_remote(**kwargs)
                with patch, mock.patch.object(cloudrun, "run_git") as git_call:
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        env = self.provisioner._push_git_refs(
                            self.store.job(self.submit_git()["id"]))
                self.assertEqual(env, {})
                git_call.assert_not_called()
                self.assertEqual(len(calls), 1)
                lines = out.getvalue().splitlines()
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("could not verify that the repository is private",
                              lines[0])

    def test_verdict_is_cached_per_url(self):
        job = self.store.job(self.submit_git()["id"])
        calls, patch = self.fake_ls_remote(code=0)
        clock = [1000.0]
        with patch, mock.patch.object(cloudrun, "now", lambda: clock[0]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.provisioner._push_git_refs(job)
            clock[0] += cloudrun.VISIBILITY_TTL - 1
            self.provisioner._push_git_refs(job)
            self.assertEqual(len(calls), 1)
            clock[0] += 2
            self.provisioner._push_git_refs(job)
            self.assertEqual(len(calls), 2)

    def test_unknown_verdict_is_rechecked_sooner(self):
        job = self.store.job(self.submit_git()["id"])
        calls, patch = self.fake_ls_remote(exc=subprocess.TimeoutExpired(["git"], 20))
        clock = [1000.0]
        with patch, mock.patch.object(cloudrun, "now", lambda: clock[0]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.provisioner._push_git_refs(job)
            clock[0] += cloudrun.VISIBILITY_UNKNOWN_TTL - 1
            self.provisioner._push_git_refs(job)
            self.assertEqual(len(calls), 1)
            clock[0] += 2
            self.provisioner._push_git_refs(job)
            self.assertEqual(len(calls), 2)
        self.assertLess(cloudrun.VISIBILITY_UNKNOWN_TTL, cloudrun.VISIBILITY_TTL)

    def test_non_http_urls_skip_the_check(self):
        for url in ("git@github.com:o/r.git", "ssh://git@github.com/o/r.git",
                    "/srv/git/r.git", "file:///srv/git/r.git"):
            with self.subTest(url=url):
                with mock.patch.object(cloudrun, "anonymous_ls_remote") as check:
                    self.assertEqual(cloudrun.repo_visibility(url)[0],
                                     cloudrun.UNCHECKED)
                check.assert_not_called()

    def test_ssh_push_url_does_not_bypass_the_check(self):
        # 检查的是容器取代码的 url，不是 push_url。
        calls, patch = self.fake_ls_remote(code=0)
        with patch, contextlib.redirect_stdout(io.StringIO()):
            env = self.provisioner._push_git_refs(self.store.job(self.submit_git()["id"]))
        self.assertEqual(env, {})
        self.assertEqual(calls[0][0], self.URL)


class AnonymousLsRemoteTests(unittest.TestCase):
    def test_runs_without_any_credential_source(self):
        polluted = {"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraheader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic c2VjcmV0",
                    "GIT_CONFIG_PARAMETERS": "'credential.helper'='store'",
                    "GIT_ASKPASS": "/usr/bin/echo-password",
                    "CALLTABLE_GIT_TOKEN": "tok", "XDG_CONFIG_HOME": "/home/x/.config"}
        result = subprocess.CompletedProcess(["git"], 0, "", "")
        with mock.patch.dict(os.environ, polluted), \
                mock.patch.object(cloudrun, "run_bounded",
                                  return_value=result) as run:
            cloudrun.anonymous_ls_remote("https://git.example.invalid/o/r.git")
        argv = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        env = kwargs["env"]
        self.assertEqual(argv[0], "git")
        self.assertIn("ls-remote", argv)
        self.assertIn("https://git.example.invalid/o/r.git", argv)
        for option in ("credential.helper=", "http.extraheader="):
            self.assertEqual(argv[argv.index(option) - 1], "-c")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "false")
        for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                     "GIT_CONFIG_PARAMETERS", "CALLTABLE_GIT_TOKEN",
                     "XDG_CONFIG_HOME"):
            self.assertNotIn(name, env)
        self.assertNotIn("c2VjcmV0", " ".join(env.values()))
        # HOME 是临时空目录（不读 ~/.netrc / ~/.gitconfig），也不在仓库里跑。
        self.assertNotEqual(env["HOME"], os.path.expanduser("~"))
        self.assertEqual(kwargs["cwd"], env["HOME"])
        self.assertEqual(kwargs["timeout"], cloudrun.VISIBILITY_TIMEOUT)

    def test_really_ignores_global_config(self):
        # 真跑 git（不联网）：全局配置里的 insteadOf 若生效，会把一个不存在的
        # 本地路径改写到存在的仓库并成功；被忽略时必然失败。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            real = root / "real.git"
            run_git(root, "init", "-q", "--bare", str(real))
            alias = (root / "alias.git").as_posix()
            gitconfig = root / "gitconfig"
            gitconfig.write_text(f'[url "{real.as_posix()}"]\n'
                                 f'\tinsteadOf = {alias}\n', encoding="utf-8")
            sanity = subprocess.run(
                ["git", "ls-remote", alias],
                env={**os.environ, "GIT_CONFIG_GLOBAL": str(gitconfig)},
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(sanity.returncode, 0, sanity.stderr)
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(gitconfig)}):
                proc = cloudrun.anonymous_ls_remote(alias, timeout=20)
            self.assertNotEqual(proc.returncode, 0)

class GitRelayUrlRulesTests(unittest.TestCase):
    """评审修复 1-3：url 只能是 http(s) / 本地路径；URL 里不许有凭据；
    push_url 必须和 url 是同一个仓库。"""

    def relay(self, url, push_url=None):
        git_settings = {"url": url}
        if push_url is not None:
            git_settings["push_url"] = push_url
        return cloudrun.GitRelay.from_profile({"git": git_settings})

    def test_url_must_be_http_or_a_local_path(self):
        for url in ("git@github.com:o/r.git", "ssh://git@github.com/o/r.git",
                    "github.com:o/r.git", "git://github.com/o/r.git",
                    "ftp://github.com/o/r.git"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self.relay(url)
        for url in ("https://github.com/o/r.git", "http://git.internal/o/r",
                    "/srv/git/r.git", "file:///srv/git/r.git"):
            with self.subTest(url=url):
                self.assertEqual(self.relay(url).url, url)

    def test_ssh_or_scp_push_url_is_still_allowed(self):
        for push_url in ("git@github.com:o/r.git", "ssh://git@github.com/o/r.git",
                         "ssh://git@GitHub.com:22/o/r", "https://github.com/o/r"):
            with self.subTest(push_url=push_url):
                self.assertEqual(
                    self.relay("https://github.com/o/r.git", push_url).push_url,
                    push_url)

    def test_no_credentials_in_urls(self):
        token = "ghp_SECRETTOKEN123"
        cases = [(f"https://{token}@github.com/o/r.git", None),
                 (f"https://x-access-token:{token}@github.com/o/r.git", None),
                 ("https://user@github.com/o/r.git", None),
                 ("https://github.com/o/r.git", f"https://{token}@github.com/o/r.git"),
                 ("https://github.com/o/r.git", f"ssh://git:{token}@github.com/o/r.git"),
                 ("https://github.com/o/r.git", f"git:{token}@github.com:o/r.git")]
        for url, push_url in cases:
            with self.subTest(url=url, push_url=push_url):
                with self.assertRaises(ValueError) as ctx:
                    self.relay(url, push_url)
                self.assertNotIn(token, str(ctx.exception))

    def test_push_url_must_name_the_same_repository(self):
        url = "https://github.com/o/r.git"
        for push_url in ("git@github.com:o/other.git", "git@gitlab.com:o/r.git",
                         "https://github.com/x/r.git", "/srv/git/r.git"):
            with self.subTest(push_url=push_url):
                with self.assertRaises(ValueError) as ctx:
                    self.relay(url, push_url)
                self.assertIn("same repository", str(ctx.exception))
        for push_url in ("git@GITHUB.com:o/r", "ssh://git@github.com:22/o/r.git/",
                         "https://github.com/o/r/", "https://github.com:443/o/r"):
            with self.subTest(push_url=push_url):
                self.relay(url, push_url)
        self.relay("/srv/git/r.git", "file:///srv/git/../git/r.git/")
        with self.assertRaises(ValueError):
            self.relay("/srv/git/r.git", "/srv/git/other.git")

    def test_backslash_is_rejected(self):
        with self.assertRaises(ValueError):
            self.relay("https://github.com/o\\r.git")


class GitRelayRuleEnforcementTests(GitRelayBase):
    def write_git(self, git_settings):
        path = self.root / "profiles" / "demo@cloudrun.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["cloudrun"]["git"] = git_settings
        path.write_text(json.dumps(data), encoding="utf-8")
        from calltable.profiles import load_profiles
        self.hub.profiles = load_profiles(self.config.profiles_dir)

    def test_hub_ignores_an_ssh_url(self):
        self.write_git({"url": "git@github.com:o/demo.git"})
        with mock.patch.object(cloudrun, "run_git") as git_call, \
                mock.patch.object(cloudrun, "anonymous_ls_remote") as check:
            job, out = self.launch()
        git_call.assert_not_called()
        check.assert_not_called()
        self.assertIn(f"[cloudrun] {job['id']}: cloudrun.git ignored", out)
        self.assertNotIn("CALLTABLE_GIT_URL", self.api.runs[0]["env"])

    def test_unchecked_network_url_is_refused_even_if_validation_is_bypassed(self):
        self.write_git({"url": "git@github.com:o/demo.git"})
        with mock.patch.object(cloudrun.GitRelay, "validate"), \
                mock.patch.object(cloudrun, "run_git") as git_call:
            job, out = self.launch()
        git_call.assert_not_called()
        self.assertIn("only http(s) URLs can be checked", out)
        self.assertNotIn("CALLTABLE_GIT_URL", self.api.runs[0]["env"])
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")

    def test_token_in_url_never_reaches_the_launch(self):
        token = "ghp_SECRETTOKEN123"
        self.write_git({"url": f"https://{token}@github.com/o/demo.git"})
        with mock.patch.object(cloudrun, "run_git") as git_call:
            job, out = self.launch()
        git_call.assert_not_called()
        self.assertNotIn(token, out)
        self.assertNotIn(token, json.dumps(self.api.runs[0]["env"]))
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")

    def test_cli_rejects_ssh_url_and_mismatched_push_url(self):
        for git_settings, fragment in (
                ({"url": "git@github.com:o/demo.git"}, "must be an http(s)"),
                ({"url": "https://github.com/o/demo.git",
                  "push_url": "git@github.com:o/other.git"}, "same repository")):
            with self.subTest(git_settings=git_settings):
                self.write_git(git_settings)
                for action in (("jobs", "--project", "demo"),
                               ("image", "--project", "demo", "--context",
                                str(self.root / "ctx-rules"))):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err), \
                            mock.patch.object(cloudrun_cli.subprocess, "call") as call:
                        code, _ = CliTests.run_cli(self, *action)
                    self.assertEqual(code, 2, action)
                    call.assert_not_called()
                    self.assertIn(fragment, err.getvalue())


class GitRelayCleanupSafetyTests(GitRelayBase):
    """评审修复 4-5：先记录再推；清理超时短、按远端退避、每轮限量。"""

    def pending_row(self, job_id, push_url, ref_names=("head",)):
        self.store.cloud_record_launch(job_id, "cloudrun", cloud_job="ct-demo-light",
                                       vcpu=1, memory_gib=1)
        self.store.cloud_update(job_id, ended=now(), state="ended",
                                git_refs=json.dumps({
                                    "project": "demo", "push_url": push_url,
                                    "refs": [f"refs/calltable/jobs/{job_id}/{name}"
                                             for name in ref_names]}))

    def test_refs_are_recorded_before_the_push(self):
        seen = []
        real = cloudrun.run_git

        def spy(args, **kwargs):
            if args[0] == "push":
                row = self.store.cloud_executions()[0]
                seen.append((row["git_refs"], kwargs["timeout"]))
            return real(args, **kwargs)

        with mock.patch.object(cloudrun, "run_git", side_effect=spy):
            job, _ = self.launch()
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0][0], "")
        self.assertEqual(seen[0][1], cloudrun.GIT_PUSH_TIMEOUT)
        self.assertLessEqual(cloudrun.GIT_PUSH_TIMEOUT, 120)

    def test_push_that_landed_but_reported_failure_is_cleaned_up(self):
        real = cloudrun.run_git

        def applied_then_timed_out(args, **kwargs):
            result = real(args, **kwargs)
            if args[0] == "push" and not any(a.startswith(":") for a in args):
                raise cloudrun.CloudRunError("git push timed out after 120s")
            return result

        with mock.patch.object(cloudrun, "run_git",
                               side_effect=applied_then_timed_out):
            job, out = self.launch()
        self.assertIn("falling back to hub bundles", out)
        self.assertNotIn("CALLTABLE_GIT_URL", self.api.runs[0]["env"])
        self.assertEqual(len(self.remote_refs()), 2)  # 实际落地了
        self.close(job)
        self.assertEqual(self.remote_refs(), {})
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")

    def test_partial_push_is_cleaned_up(self):
        # 服务端只收下 head（例如 base 被拒）：记录里两个 ref 都在，清理后不剩。
        prefix_holder = {}
        real = cloudrun.run_git

        def head_only(args, **kwargs):
            if args[0] == "push" and not any(a.startswith(":") for a in args):
                heads = [a for a in args if a.endswith("/head")]
                prefix_holder["head"] = heads[0]
                real([*args[:4], *heads], **kwargs)
                raise cloudrun.CloudRunError("! [remote rejected] .../base")
            return real(args, **kwargs)

        with mock.patch.object(cloudrun, "run_git", side_effect=head_only):
            job, _ = self.launch()
        self.assertEqual(len(self.remote_refs()), 1)
        out = self.close(job)
        self.assertEqual(self.remote_refs(), {})
        self.assertNotIn("will retry", out)

    def test_cleanup_uses_short_timeouts(self):
        self.pending_row("job-x", str(self.remote))
        timeouts = []
        real = cloudrun.run_git

        def spy(args, **kwargs):
            timeouts.append((args[0], kwargs["timeout"]))
            if args[0] == "push":
                raise cloudrun.CloudRunError("remote ref does not exist")
            return real(args, **kwargs)

        with mock.patch.object(cloudrun, "run_git", side_effect=spy):
            self.provisioner._cleanup_git_refs("cloudrun")
        # 删除报错 → ls-remote 核对（远端没有这个 ref）→ 视为已删。
        self.assertEqual(timeouts, [("push", cloudrun.GIT_DELETE_TIMEOUT),
                                    ("ls-remote", cloudrun.GIT_LS_REMOTE_TIMEOUT)])
        self.assertLessEqual(cloudrun.GIT_DELETE_TIMEOUT, 60)
        self.assertLessEqual(cloudrun.GIT_LS_REMOTE_TIMEOUT, 30)
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")

    def test_backoff_is_per_remote(self):
        dead = str(self.root / "gone.git")
        for index in range(3):
            self.pending_row(f"job-dead-{index}", dead)
        self.pending_row("job-alive", str(self.remote))
        calls = []
        real = cloudrun.run_git

        def spy(args, **kwargs):
            calls.append(args)
            return real(args, **kwargs)

        out = io.StringIO()
        with mock.patch.object(cloudrun, "run_git", side_effect=spy), \
                contextlib.redirect_stdout(out):
            self.provisioner._cleanup_git_refs("cloudrun")
        dead_pushes = [c for c in calls if c[0] == "push" and dead in c]
        self.assertEqual(len(dead_pushes), 1)  # 其余两条同远端的记录被跳过
        self.assertEqual(out.getvalue().count("will retry"), 1)
        pending = {row["job_id"] for row in self.store.cloud_git_pending("cloudrun")}
        self.assertEqual(pending, {f"job-dead-{i}" for i in range(3)})
        # 退避期内再来一轮：死远端一次都不碰
        calls.clear()
        with mock.patch.object(cloudrun, "run_git", side_effect=spy):
            self.provisioner._cleanup_git_refs("cloudrun")
        self.assertEqual(calls, [])

    def test_one_pass_is_bounded(self):
        total = cloudrun.GIT_CLEANUP_BATCH * 2 + 3
        for index in range(total):
            self.pending_row(f"job-{index:03d}", str(self.remote))
        with mock.patch.object(self.provisioner, "_delete_remote_refs") as delete:
            self.provisioner._cleanup_git_refs("cloudrun")
            self.assertEqual(delete.call_count, cloudrun.GIT_CLEANUP_BATCH)
            self.provisioner._cleanup_git_refs("cloudrun")
            self.assertEqual(delete.call_count, cloudrun.GIT_CLEANUP_BATCH * 2)
        self.assertEqual(len(self.store.cloud_git_pending("cloudrun")), 3)

class GitRelayFollowUpTests(unittest.TestCase):
    """复审跟进 2：ssh / scp 形式的用户名只能是常规短账号名。"""

    TOKEN_USER = "ghp_AbCdEf0123456789abcdefABCDEF012345"

    def relay(self, push_url):
        return cloudrun.GitRelay.from_profile({"git": {
            "url": "https://github.com/o/r.git", "push_url": push_url}})

    def test_token_shaped_ssh_user_is_rejected(self):
        for push_url in (f"{self.TOKEN_USER}@github.com:o/r.git",
                         f"ssh://{self.TOKEN_USER}@github.com/o/r.git",
                         "tok@github.com:o/r.git".replace("tok", "Tok"),
                         "x" * 33 + "@github.com:o/r.git",
                         "1git@github.com:o/r.git"):
            with self.subTest(push_url=push_url):
                with self.assertRaises(ValueError) as ctx:
                    self.relay(push_url)
                message = str(ctx.exception)
                self.assertIn("short account name", message)
                self.assertNotIn(push_url.split("@")[0].split("//")[-1], message)
        self.assertNotIn(self.TOKEN_USER,
                         cloudrun.redact_url(f"{self.TOKEN_USER}@github.com:o/r"))

    def test_conventional_users_still_work(self):
        for push_url in ("git@github.com:o/r.git", "ssh://git@github.com/o/r.git",
                         "deploy_bot-2@github.com:o/r.git", "github.com:o/r.git"):
            with self.subTest(push_url=push_url):
                self.assertEqual(self.relay(push_url).push_url, push_url)


@unittest.skipIf(os.name == "nt", "process groups are POSIX only")
class ProcessGroupTimeoutTests(unittest.TestCase):
    """复审跟进 3：超时后整个进程组都被杀掉（git 拉起的传输子进程不会留下）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pidfile = self.root / "child.pid"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        # 假 git：像 git-remote-https 一样拉起一个子进程，然后一起卡住。
        # 超时给 5 秒：新写出的脚本第一次启动可能要一秒多（macOS 会先扫描）。
        fake = bin_dir / "git"
        fake.write_text("#!/bin/sh\n"
                        f"sleep 300 &\necho $! > '{self.pidfile}'\nwait\n",
                        encoding="utf-8")
        fake.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def child_pid(self) -> int:
        wait_until(lambda: self.pidfile.exists()
                   and self.pidfile.read_text().strip(), timeout=10)
        return int(self.pidfile.read_text().strip())

    def assert_gone(self, pid: int) -> None:
        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            try:  # Linux：已退出、等待被 init 回收的僵尸也算没了
                with open(f"/proc/{pid}/stat") as handle:
                    return handle.read().split()[2] == "Z"
            except OSError:
                return False
        wait_until(gone, timeout=10)

    def test_run_git_timeout_kills_the_transport_child(self):
        started = time.time()
        with mock.patch.dict(os.environ, {"PATH": self.path}):
            with self.assertRaises(cloudrun.CloudRunError) as ctx:
                cloudrun.run_git(["push", "x"], cwd=self.root, timeout=5)
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(time.time() - started, 20)
        self.assert_gone(self.child_pid())

    def test_visibility_timeout_kills_the_transport_child(self):
        with mock.patch.dict(os.environ, {"PATH": self.path}), \
                mock.patch.object(cloudrun, "VISIBILITY_TIMEOUT", 5):
            verdict, detail = cloudrun.repo_visibility("https://git.example.invalid/r")
        self.assertEqual(verdict, cloudrun.UNKNOWN)
        self.assertIn("timed out", detail)
        self.assert_gone(self.child_pid())

    def test_stdin_is_the_null_device(self):
        proc = cloudrun.run_bounded(["sh", "-c", "cat; echo done"], cwd=self.root,
                                    env=dict(os.environ), timeout=10)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "done"))


class GitRelayGiveUpTests(GitRelayBase):
    """复审跟进 1：execution 记录关闭超过 24 小时仍删不掉，就放弃重试。"""

    def pending_row(self, job_id, push_url, ended):
        self.store.cloud_record_launch(job_id, "cloudrun", cloud_job="ct-demo-light",
                                       vcpu=1, memory_gib=1, launched=ended - 60)
        self.store.cloud_update(job_id, ended=ended, state="ended",
                                note="succeeded=1",
                                git_refs=json.dumps({
                                    "project": "demo", "push_url": push_url,
                                    "refs": [f"refs/calltable/jobs/{job_id}/head"]}))

    def row(self, job_id):
        return next(r for r in self.store.cloud_executions() if r["job_id"] == job_id)

    def test_unreachable_remote_is_given_up_after_a_day(self):
        ended = 1_000_000.0
        dead_old = str(self.root / "gone-old.git")
        dead_young = str(self.root / "gone-young.git")
        self.pending_row("job-old", dead_old, ended)
        self.pending_row("job-young", dead_young,
                         ended + cloudrun.GIT_CLEANUP_MAX_AGE - 3600)
        clock = [ended + cloudrun.GIT_CLEANUP_MAX_AGE + 1]
        out = io.StringIO()
        with mock.patch.object(cloudrun, "now", lambda: clock[0]), \
                contextlib.redirect_stdout(out):
            self.provisioner._cleanup_git_refs("cloudrun")
        text = out.getvalue()
        give_up = [line for line in text.splitlines() if "giving up" in line]
        self.assertEqual(len(give_up), 1, text)
        self.assertTrue(give_up[0].startswith("[cloudrun] job-old: "))
        self.assertIn(dead_old, give_up[0])
        self.assertIn("refs/calltable/jobs/job-old/head", give_up[0])
        old = self.row("job-old")
        self.assertEqual(old["git_refs"], "")
        self.assertIn("refs/calltable/jobs/job-old/head", old["note"])
        self.assertIn(dead_old, old["note"])
        self.assertIn("succeeded=1", old["note"])
        # 还没满 24 小时的照旧重试
        young = self.row("job-young")
        self.assertNotEqual(young["git_refs"], "")
        self.assertIn("job-young: deleting git refs", text)
        self.assertIn("will retry", text)
        self.assertEqual({r["job_id"] for r in self.store.cloud_git_pending("cloudrun")},
                         {"job-young"})
        # 放弃之后不再碰它
        clock[0] += cloudrun.GIT_CLEANUP_RETRY + 1
        with mock.patch.object(cloudrun, "now", lambda: clock[0]), \
                mock.patch.object(self.provisioner, "_delete_remote_refs",
                                  side_effect=cloudrun.CloudRunError("down")) as delete, \
                contextlib.redirect_stdout(io.StringIO()):
            self.provisioner._cleanup_git_refs("cloudrun")
        self.assertEqual([c.args[1] for c in delete.call_args_list], [dead_young])

    def test_reachable_old_row_is_still_deleted_normally(self):
        ended = 1_000_000.0
        self.pending_row("job-ok", str(self.remote), ended)
        with mock.patch.object(cloudrun, "now",
                               lambda: ended + cloudrun.GIT_CLEANUP_MAX_AGE * 3), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.provisioner._cleanup_git_refs("cloudrun")
        self.assertEqual(self.row("job-ok")["git_refs"], "")
        self.assertNotIn("giving up", out.getvalue())
        self.assertEqual(self.row("job-ok")["note"], "succeeded=1")

class GitRelayOffTests(GitRelayBase):
    def profile_git(self):
        self.remote = Path(self.tmp.name) / "remote.git"
        return None

    def test_without_cloudrun_git_nothing_changes(self):
        with mock.patch.object(cloudrun, "run_git") as git_call:
            job, out = self.launch()
            self.close(job)
        git_call.assert_not_called()
        env = self.api.runs[0]["env"]
        self.assertEqual(sorted(env), ["CALLTABLE_EXEC_HOST", "CALLTABLE_HUB_URL",
                                       "CALLTABLE_JOB_ID"])
        self.assertEqual(out.splitlines(), [
            f"[cloudrun] {job['id']} -> ct-demo-light execution "
            f"{self.api.runs[0]['execution']}"])
        self.assertEqual(self.store.cloud_executions()[0]["git_refs"], "")
        self.assertEqual(self.remote_refs(), {})


class GitRelayMigrationTests(unittest.TestCase):
    def test_old_database_gains_the_git_refs_column(self):
        import sqlite3
        from calltable.db import Store
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE cloud_executions (
                job_id TEXT PRIMARY KEY, host TEXT NOT NULL,
                cloud_job TEXT NOT NULL DEFAULT '', execution TEXT NOT NULL DEFAULT '',
                vcpu REAL NOT NULL DEFAULT 0, memory_gib REAL NOT NULL DEFAULT 0,
                launched REAL NOT NULL, attached REAL, last_seen REAL, ended REAL,
                state TEXT NOT NULL DEFAULT 'launching', note TEXT NOT NULL DEFAULT '')""")
            conn.execute("INSERT INTO cloud_executions(job_id, host, launched, ended) "
                         "VALUES('j1', 'cloudrun', 1, 2)")
            conn.commit()
            conn.close()
            store = Store(path)
            try:
                self.assertEqual(store.cloud_executions()[0]["git_refs"], "")
                self.assertEqual(store.cloud_git_pending("cloudrun"), [])
                store.cloud_update("j1", git_refs="{}")
                self.assertEqual(len(store.cloud_git_pending("cloudrun")), 1)
            finally:
                store.close()


class GitRelayCliTests(Base):
    def profile_git(self):
        return {"url": "https://github.com/o/demo.git",
                "push_url": "git@github.com:o/demo.git",
                "branch": "trunk", "token_secret": "calltable-git-demo"}

    run_cli = CliTests.run_cli

    def test_jobs_mount_the_git_token_secret(self):
        code, out = self.run_cli("jobs", "--project", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(out.count(
            "CALLTABLE_GIT_TOKEN=calltable-git-demo:latest"), 2)

    def test_job_commands_secrets(self):
        settings = self.provisioner.settings["cloudrun"]
        url = "https://github.com/o/demo.git"

        def secrets(cloudrun_profile, host_default=""):
            settings.git_token_secret = host_default
            cmd = cloudrun_cli.job_commands(settings, "demo", "img", ["light"],
                                            cloudrun_profile)[0]
            return next(a for a in cmd if a.startswith("--set-secrets="))

        plain = secrets(None)
        self.assertNotIn("CALLTABLE_GIT_TOKEN", plain)
        self.assertEqual(secrets({"base_image": "x"}), plain)
        self.assertEqual(secrets({"git": {"url": url}}), plain)
        self.assertIn("CALLTABLE_GIT_TOKEN=shared:latest",
                      secrets({"git": {"url": url}}, "shared"))
        self.assertIn("CALLTABLE_GIT_TOKEN=mine:latest",
                      secrets({"git": {"url": url, "token_secret": "mine"}},
                              "shared"))
        self.assertEqual(secrets({"git": {"url": url, "token_secret": ""}},
                                 "shared"), plain)
        # 原命令（不带 profile 参数）不变
        self.assertEqual(
            cloudrun_cli.job_commands(settings, "demo", "img", ["light"]),
            cloudrun_cli.job_commands(settings, "demo", "img", ["light"], {}))

    def test_image_with_cloudbuild_bakes_the_mirror(self):
        context = self.root / "ctx"
        with mock.patch.object(cloudrun_cli.subprocess, "call") as call:
            code, out = self.run_cli("image", "--project", "demo",
                                     "--context", str(context), "--tag", "v1")
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertIn("will bake branch trunk of https://github.com/o/demo.git",
                      out)
        self.assertIn(f"gcloud builds submit {context} "
                      f"--config={context / 'cloudbuild.yaml'} "
                      "--project=demo-project --region=us-central1 "
                      "--substitutions=_IMAGE=us-central1-docker.pkg.dev/"
                      "demo-project/calltable/demo:v1", out)
        self.assertNotIn("--tag=", out)
        self.assertIn("dry run", out)
        dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
        plain = cloudrun_cli.render_dockerfile(
            {k: v for k, v in self.profile_cloudrun().items() if k != "git"})
        self.assertEqual(dockerfile.replace(
            "COPY mirrors/ /opt/calltable/mirrors/\n", "", 1), plain)
        self.assertIn("COPY calltable /opt/calltable/calltable\n"
                      "COPY mirrors/ /opt/calltable/mirrors/\n", dockerfile)
        yaml = (context / "cloudbuild.yaml").read_text(encoding="utf-8")
        steps = yaml.index("- id: bake-mirror"), yaml.index("- id: build")
        self.assertLess(*steps)
        self.assertIn("git clone --bare --single-branch --no-tags --branch 'trunk' "
                      "'https://github.com/o/demo.git' 'mirrors/demo.git'", yaml)
        self.assertIn("secretEnv:\n  - GIT_TOKEN", yaml)
        self.assertIn("versionName: projects/demo-project/secrets/"
                      "calltable-git-demo/versions/latest", yaml)
        self.assertIn("GIT_CONFIG_KEY_0=http.extraheader", yaml)
        self.assertIn("- $_IMAGE", yaml)
        # Cloud Build 会替换 $NAME：shell 变量必须是 $$，唯一的单 $ 是 $_IMAGE。
        import re
        singles = re.findall(r"(?<!\$)\$(?!\$)[{(A-Za-z_]+", yaml)
        self.assertEqual(set(singles), {"$_IMAGE"}, singles)

    def test_docker_builder_does_not_bake(self):
        context = self.root / "ctx-docker"
        code, out = self.run_cli("image", "--project", "demo", "--context",
                                 str(context), "--builder", "docker")
        self.assertEqual(code, 0)
        self.assertEqual(sum("needs --builder cloudbuild" in line
                             for line in out.splitlines()), 1)
        self.assertIn("docker build --platform=linux/amd64", out)
        self.assertFalse((context / "cloudbuild.yaml").exists())
        self.assertNotIn("mirrors", (context / "Dockerfile").read_text())

    def test_invalid_git_settings_are_a_usage_error(self):
        path = self.root / "profiles" / "demo@cloudrun.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["cloudrun"]["git"]["branch"] = "x; rm"
        path.write_text(json.dumps(data), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cli("image", "--project", "demo", "--context",
                                   str(self.root / "ctx-bad"))
        self.assertEqual(code, 2)
        self.assertIn("cloudrun.git.branch", err.getvalue())

    def profile_cloudrun(self):
        path = self.root / "profiles" / "demo@cloudrun.json"
        return json.loads(path.read_text(encoding="utf-8"))["cloudrun"]


class GitRelayImageTests(unittest.TestCase):
    """没有 cloudrun.git 时 Dockerfile / 命令与以前逐字节相同；预置脚本能真跑。"""

    def setUp(self):
        self.settings = CloudRunSettings.from_host("cr", {"project": "p"})

    def test_plain_image_is_unchanged(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            context = Path(tmp)
            (context / "cloudbuild.yaml").write_text("stale", encoding="utf-8")
            cloudrun_cli.write_build_context({"base_image": "b"}, context)
            self.assertFalse((context / "cloudbuild.yaml").exists())
            dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
        template = (cloudrun_cli.ASSETS / "Dockerfile.template").read_text(
            encoding="utf-8")
        expected = (template.replace("ARG BASE_IMAGE=python:3.12-slim",
                                     "ARG BASE_IMAGE=b", 1)
                    .replace("# @@INSTALL@@", "# (no profile install steps)", 1)
                    .replace("# @@ENV@@", "# (no profile image env)", 1))
        self.assertEqual(dockerfile, expected)
        self.assertEqual(
            cloudrun_cli.image_commands(self.settings, "img", Path("/ctx"),
                                        "cloudbuild"),
            [["gcloud", "builds", "submit", "/ctx", "--project=p",
              "--region=us-central1", "--tag=img"]])

    def test_no_token_means_no_secret_section(self):
        relay = cloudrun.GitRelay.from_profile(
            {"git": {"url": "https://github.com/o/r.git"}})
        yaml = cloudrun_cli.render_cloudbuild(self.settings, "demo", relay)
        self.assertNotIn("availableSecrets", yaml)
        self.assertNotIn("secretEnv", yaml)
        self.assertIn("--branch 'main'", yaml)
        with self.assertRaises(cloudrun_cli.UsageError):
            cloudrun_cli.render_cloudbuild(self.settings, "../x", relay)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "the bake step is a bash script")
    def test_bake_script_clones_a_clean_bare_mirror(self):
        token = "ghp-bake-token-never-in-config"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            run_git(src, "init", "-q", "-b", "trunk")
            (src / "f").write_text("x\n", encoding="utf-8")
            run_git(src, "add", "-A")
            run_git(src, "commit", "-qm", "x")
            run_git(src, "branch", "other")
            remote = root / "remote.git"
            run_git(root, "clone", "-q", "--bare", str(src), str(remote))
            relay = cloudrun.GitRelay.from_profile({"git": {
                "url": str(remote), "branch": "trunk", "token_secret": "t"}})
            script = "\n".join(cloudrun_cli._bake_script("demo", relay))
            # Cloud Build 把 $$ 还原成 $ 再交给 bash。
            script = script.replace("$$", "$")
            workspace = root / "workspace"
            workspace.mkdir()
            proc = subprocess.run(["bash", "-c", script], cwd=str(workspace),
                                  capture_output=True, text=True,
                                  env={**os.environ, "GIT_TOKEN": token})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            mirror = workspace / "mirrors" / "demo.git"
            self.assertEqual(run_git(mirror, "rev-parse", "--is-bare-repository"),
                             "true")
            heads = run_git(mirror, "for-each-ref", "--format=%(refname)")
            self.assertEqual(heads.split(), ["refs/heads/trunk"])
            config = (mirror / "config").read_text(encoding="utf-8")
            self.assertNotIn(token, config)
            self.assertNotIn("extraheader", config)
            self.assertNotIn("remote", config)
            self.assertNotIn(token, proc.stdout + proc.stderr)


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"),
                 "the Cloud Run entrypoint is a POSIX shell script")
class EntrypointMirrorTests(unittest.TestCase):
    def run_entrypoint(self, mirror_root: Path) -> dict:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            out = root / "config-seen.json"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "python3"
            # entrypoint: exec python3 -m calltable exec --config <path> ...
            fake.write_text(f'#!/bin/sh\ncp "$5" "{out}"\n', encoding="utf-8")
            fake.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k != "TS_AUTHKEY"}
            env.update({"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                        "CALLTABLE_JOB_ID": "job-1",
                        "CALLTABLE_HUB_URL": "http://127.0.0.1:1",
                        "CALLTABLE_TOKEN": "t",
                        "CALLTABLE_WORK_DIR": str(root / "work"),
                        "CALLTABLE_MIRROR_ROOT": str(mirror_root)})
            entry = cloudrun_cli.ASSETS / "entrypoint.sh"
            proc = subprocess.run(["sh", str(entry)], env=env,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(out.read_text(encoding="utf-8"))
            data["_work"] = str(root / "work")
            return data

    def test_baked_mirror_directory_is_used_when_present(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            baked = Path(tmp) / "mirrors"
            baked.mkdir()
            config = self.run_entrypoint(baked)
        self.assertEqual(config["mirror_root"], str(baked))

    def test_work_dir_mirrors_without_a_baked_directory(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = self.run_entrypoint(Path(tmp) / "absent")
        self.assertEqual(config["mirror_root"],
                         config["_work"] + "/calltable/mirrors")


if __name__ == "__main__":
    unittest.main()
