"""Cloud Run provider：预留 + attach、按 worker 对账、花费封顶、execution 对账、
镜像/job 定义命令。全部用假的 Cloud Run API，不调用 gcloud、不产生费用。"""

from __future__ import annotations

import io
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from calltable import cloudrun
from calltable import cloudrun_cli
from calltable.client import HubClient
from calltable.cloudrun import CloudRunSettings, Provisioner
from calltable.db import PENDING_WORKER_PREFIX
from calltable.hub import Hub
from calltable.util import now
from tests.helpers import make_config

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


class Base(unittest.TestCase):
    hosts_overrides: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root, hosts=cloud_hosts(**self.hosts_overrides))
        (self.root / "profiles" / "demo@cloudrun.json").write_text(json.dumps({
            "name": "demo@cloudrun", "slot": "light", "timeout": "20m",
            "cloudrun": {"base_image": "mcr.microsoft.com/dotnet/sdk:10.0",
                         "install": ["apt-get update && apt-get install -y jq"],
                         "env": {"DOTNET_NOLOGO": "1"}},
            "rules": [{"match": "gate", "slot": "heavy", "timeout": "2h"}],
        }), encoding="utf-8")
        self.api = FakeAPI()
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
        self.assertEqual(settings.monthly_budget_usd, 60.0)
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


if __name__ == "__main__":
    unittest.main()
