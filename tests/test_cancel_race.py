"""取消与领取/准备/对账的竞态（issue #1）：取消优先，终态一律 ``cancelled``。

不靠 sleep 卡时间：hub 侧直接按顺序驱动 Store（claim → cancel → 对账/上报），
执行器侧用 FakeClient 和准备阶段的打桩把取消精确放在领取之后、拉起之前。
"""

from __future__ import annotations

import contextlib
import http.client
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from calltable import executor as executor_module
from calltable.db import MISSING_STRIKES, Store
from calltable.executor import Executor
from calltable.gitops import GitError
from calltable.util import now
from tests.helpers import make_config
from tests.test_executor import FakeClient


class StoreCancelRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "hub.sqlite3")
        self.addCleanup(self.store.close)
        self.store.seed_host("h1", {"light": 1}, [])
        self.store.heartbeat("h1", "w1", [])

    def _job(self, **kw):
        fields = dict(project="p", host="h1", slot="light", locks=[],
                      cmd=["true"])
        fields.update(kw)
        return self.store.create_job(**fields)

    def _claimed_then_cancelled(self, **kw):
        job = self._job(**kw)
        claimed = self.store.claim("h1", "w1")
        self.assertEqual(claimed["id"], job["id"])
        cancelled = self.store.request_cancel(job["id"])
        self.assertEqual(cancelled["status"], "running")
        self.assertTrue(cancelled["cancel_requested"])
        return job

    def _assert_cancelled(self, job_id, note):
        job = self.store.job(job_id)
        self.assertEqual(job["status"], "cancelled", job)
        self.assertEqual(job["exit_code"], -2)
        self.assertIn(note, job["error"])
        return job

    # ------------------------------------------------------------ 领取之前
    def test_cancel_just_before_claim_is_terminal_and_never_claimed(self):
        job = self._job()
        self.store.request_cancel(job["id"])
        self.assertIsNone(self.store.claim("h1", "w1"))
        job = self.store.job(job["id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["exit_code"], -2)

    # ------------------------------------------------------------ 领取之后、执行器登记之前
    def test_heartbeat_missing_job_after_cancel_is_cancelled(self):
        job = self._claimed_then_cancelled()
        for _ in range(MISSING_STRIKES):
            self.store.heartbeat("h1", "w1", [])
        self._assert_cancelled(job["id"], "missing from executor heartbeat")

    def test_worker_change_after_cancel_is_cancelled(self):
        job = self._claimed_then_cancelled()
        reaped = self.store.heartbeat("h1", "w2", [])
        self.assertEqual([row["id"] for row in reaped], [job["id"]])
        self.assertEqual(reaped[0]["status"], "cancelled")
        self._assert_cancelled(job["id"], "executor worker changed")

    def test_stale_host_after_cancel_is_cancelled(self):
        job = self._claimed_then_cancelled()
        self.store.conn.execute("UPDATE hosts SET last_seen=? WHERE name='h1'",
                                (now() - 3600,))
        self.store.reap_stale_hosts(stale_seconds=60)
        self._assert_cancelled(job["id"], "heartbeat lost")

    def test_provider_reconcile_error_after_cancel_is_cancelled(self):
        job = self._claimed_then_cancelled()
        self.store.finish_if_status(job["id"], ("running",), status="error",
                                    exit_code=None, error="execution vanished")
        self._assert_cancelled(job["id"], "execution vanished")

    # ------------------------------------------------------------ 执行器上报
    def test_executor_error_after_cancel_is_cancelled(self):
        job = self._claimed_then_cancelled()
        self.store.finish(job["id"], status="error", exit_code=None,
                          error="GitError: clone failed")
        job = self._assert_cancelled(job["id"], "GitError: clone failed")
        self.assertIn("reported error", job["error"])

    def test_executor_success_after_cancel_is_cancelled_keeping_exit_code(self):
        job = self._claimed_then_cancelled()
        self.store.finish(job["id"], status="succeeded", exit_code=0)
        job = self.store.job(job["id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["exit_code"], 0)
        self.assertIn("reported succeeded", job["error"])

    def test_executor_cancelled_report_is_unchanged(self):
        job = self._claimed_then_cancelled()
        self.store.finish(job["id"], status="cancelled", exit_code=-15)
        job = self.store.job(job["id"])
        self.assertEqual((job["status"], job["exit_code"], job["error"]),
                         ("cancelled", -15, ""))

    def test_executor_cancelled_report_without_exit_code_is_minus_two(self):
        """执行器在准备阶段中止时报 cancelled、exit_code 为空：按规范记 -2。"""
        job = self._claimed_then_cancelled()
        self.store.finish(job["id"], status="cancelled", exit_code=None,
                          error="aborted during preparation")
        job = self.store.job(job["id"])
        self.assertEqual((job["status"], job["exit_code"], job["error"]),
                         ("cancelled", -2, "aborted during preparation"))

    def test_executor_cancelled_report_with_minus_one_is_minus_two(self):
        job = self._claimed_then_cancelled()
        self.store.finish(job["id"], status="cancelled", exit_code=-1,
                          error="cancel requested; GitError: x")
        job = self.store.job(job["id"])
        self.assertEqual((job["status"], job["exit_code"]), ("cancelled", -2))
        self.assertEqual(job["error"], "cancel requested; GitError: x")

    def test_cancelled_without_cancel_request_keeps_exit_code(self):
        """没请求过取消的任务不走取消优先，退出码原样保存。"""
        job = self._job()
        self.store.claim("h1", "w1")
        self.store.finish(job["id"], status="cancelled", exit_code=None)
        job = self.store.job(job["id"])
        self.assertEqual((job["status"], job["exit_code"]), ("cancelled", None))

    def _cleanup_at(self, job_id):
        return self.store.conn.execute(
            "SELECT cleanup_at FROM jobs WHERE id=?", (job_id,)).fetchone()[0]

    def test_retention_follows_final_status_when_cancel_wins(self):
        """hub 按「succeeded」选了成功保留期，但取消先到：改用失败保留期。"""
        job = self._claimed_then_cancelled()
        self.store.finish(job["id"], status="succeeded", exit_code=0,
                          cleanup_at=100.0, failure_cleanup_at=300.0)
        self.assertEqual(self.store.job(job["id"])["status"], "cancelled")
        self.assertEqual(self._cleanup_at(job["id"]), 300.0)

    def test_retention_success_kept_without_cancel(self):
        job = self._job()
        self.store.claim("h1", "w1")
        self.store.finish(job["id"], status="succeeded", exit_code=0,
                          cleanup_at=100.0, failure_cleanup_at=300.0)
        self.assertEqual(self.store.job(job["id"])["status"], "succeeded")
        self.assertEqual(self._cleanup_at(job["id"]), 100.0)

    def test_without_cancel_error_stays_error(self):
        job = self._job()
        self.store.claim("h1", "w1")
        for _ in range(MISSING_STRIKES):
            self.store.heartbeat("h1", "w1", [])
        job = self.store.job(job["id"])
        self.assertEqual((job["status"], job["exit_code"]), ("error", -1))

    def test_finished_event_reports_cancelled_once(self):
        job = self._claimed_then_cancelled()
        self.store.heartbeat("h1", "w2", [])
        # 执行器稍后重放 error：仍是 cancelled，也不再发第二条 job.finished。
        self.store.finish(job["id"], status="error", exit_code=None, error="late")
        finished = [event for event in self.store.events_after(0, job["id"])
                    if event["type"] == "job.finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["job"]["status"], "cancelled")
        self.assertEqual(self.store.job(job["id"])["status"], "cancelled")


class AttachClient(FakeClient):
    """一次性执行器（Cloud Run）attach 时 hub 返回的任务快照。"""

    def __init__(self, job):
        super().__init__()
        self.attach_job = job

    def post_json(self, path, body=None):
        if path.endswith("/attach"):
            return self.attach_job
        return super().post_json(path, body)


class ExecutorCancelRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)

    def _job(self, job_id, **kw):
        job = {"id": job_id, "project": "demo", "host": "h1", "sha": "c" * 40,
               "timeout": 60, "profile_data": {}, "cmd": ["true"],
               "bundle_url": "", "submitter": "t", "dirty": False}
        job.update(kw)
        return job

    def _finish(self, client):
        bodies = [body for path, body in client.posts if path.endswith("/finish")]
        self.assertEqual(len(bodies), 1, client.posts)
        return bodies[0]

    @contextlib.contextmanager
    def _prepared(self, exe, clone=None):
        """准备阶段打桩到「拉起之前」；真的拉起进程就算失败。"""
        with mock.patch.object(executor_module.gitops, "has_commit",
                               return_value=True), \
                mock.patch.object(executor_module.gitops, "clone_shared",
                                  side_effect=clone), \
                mock.patch.object(exe, "_write_launcher",
                                  return_value=self.root / "run.sh"), \
                mock.patch.object(exe, "_build_env", return_value={}), \
                mock.patch.object(exe, "_argv", return_value=["true"]), \
                mock.patch.object(executor_module.proc, "spawn",
                                  side_effect=AssertionError("launched")) as spawn:
            yield spawn

    def test_cancel_in_claimed_snapshot_aborts_before_any_work(self):
        job = self._job("job-attach", cancel_requested=True,
                        bundle_url="/api/jobs/job-attach/bundle")
        client = AttachClient(job)
        exe = Executor(self.config, client=client, job_id="job-attach")
        self.addCleanup(exe.stop)
        with self._prepared(exe) as spawn:
            exe._claim_loop()
        spawn.assert_not_called()
        self.assertEqual(client.downloads, [])
        finish = self._finish(client)
        self.assertEqual(finish["status"], "cancelled")
        self.assertIn("preparation", finish["error"])

    def test_cancel_during_preparation_is_seen_before_launch(self):
        client = FakeClient()
        exe = Executor(self.config, client=client)
        self.addCleanup(exe.stop)

        def clone(mirror, work, sha):
            # 取消在准备阶段到达 hub；取消轮询还没来得及跑。
            client.cancel_jobs.add("job-prep")

        with self._prepared(exe, clone=clone) as spawn:
            exe._run_job(self._job("job-prep"))
        spawn.assert_not_called()
        finish = self._finish(client)
        self.assertEqual(finish["status"], "cancelled")
        self.assertIn("preparation", finish["error"])

    def test_preparation_failure_after_cancel_reports_cancelled(self):
        client = FakeClient()
        exe = Executor(self.config, client=client)
        self.addCleanup(exe.stop)

        def clone(mirror, work, sha):
            exe._cancel_flags["job-fail"] = True
            raise GitError("clone interrupted")

        with self._prepared(exe, clone=clone) as spawn:
            exe._run_job(self._job("job-fail"))
        spawn.assert_not_called()
        finish = self._finish(client)
        self.assertEqual(finish["status"], "cancelled")
        self.assertIn("clone interrupted", finish["error"])

    def test_preparation_failure_without_cancel_is_still_error(self):
        client = FakeClient()
        exe = Executor(self.config, client=client)
        self.addCleanup(exe.stop)
        with self._prepared(exe, clone=GitError("clone failed")):
            exe._run_job(self._job("job-err"))
        finish = self._finish(client)
        self.assertEqual(finish["status"], "error")
        self.assertIn("clone failed", finish["error"])

    def _launches_despite_lookup_failure(self, job_id, get_json):
        """拉起前查取消失败（hub 断开、超时、非 JSON）：照常拉起，不报 error。"""
        client = FakeClient()
        client.get_json = get_json
        exe = Executor(self.config, client=client)
        self.addCleanup(exe.stop)
        launched = AssertionError("launched")
        with self._prepared(exe) as spawn:
            # 拉起之后的监督不在本用例范围内：spawn 抛出哨兵异常即说明已拉起。
            spawn.side_effect = launched
            exe._run_job(self._job(job_id))
        spawn.assert_called_once()
        finish = self._finish(client)
        # 唯一的失败来自哨兵（拉起之后），不是查取消那一步。
        self.assertEqual(finish["status"], "error")
        self.assertIn("launched", finish["error"])
        self.assertNotIn(job_id, exe._cancel_flags)
        return finish

    def test_hub_disconnect_before_launch_still_launches(self):
        calls = []

        def get_json(path, params=None, **kw):
            calls.append(kw)
            raise http.client.RemoteDisconnected("closed without response")

        finish = self._launches_despite_lookup_failure("job-rd", get_json)
        self.assertNotIn("RemoteDisconnected", finish["error"])
        # 拉起前那次查询带短超时。
        self.assertEqual(calls, [{"timeout": 10}])

    def test_hub_timeout_before_launch_still_launches(self):
        def get_json(path, params=None, **kw):
            raise TimeoutError("timed out")

        finish = self._launches_despite_lookup_failure("job-to", get_json)
        self.assertNotIn("TimeoutError", finish["error"])

    def test_non_json_answer_before_launch_still_launches(self):
        def get_json(path, params=None, **kw):
            return b"<html>proxy page</html>"

        finish = self._launches_despite_lookup_failure("job-bytes", get_json)
        self.assertNotIn("AttributeError", finish["error"])

    def test_watch_loop_survives_hub_disconnect(self):
        """取消轮询遇到 RemoteDisconnected / 超时只跳过一轮，下一轮照常查。"""
        client = FakeClient()
        results = [http.client.RemoteDisconnected("closed"),
                   TimeoutError("timed out"),
                   b"not json",
                   {"cancel_requested": True}]
        calls = []

        def get_json(path, params=None, **kw):
            calls.append(path)
            result = results[len(calls) - 1]
            if isinstance(result, BaseException):
                raise result
            return result

        client.get_json = get_json
        exe = Executor(self.config, client=client)
        self.addCleanup(exe.stop)
        with exe._lock:
            exe._active_jobs.add("job-watch")
        # 不真的等 3 秒：前四轮 wait 立即返回「未停止」，第五轮停止。
        stop = mock.Mock()
        stop.wait.side_effect = [False, False, False, False, True]
        with mock.patch.object(exe, "_stop", stop):
            exe._watch_loop()
        self.assertEqual(calls, ["/api/jobs/job-watch"] * 4)
        self.assertTrue(exe._cancel_flags.get("job-watch"))

    def test_finished_job_is_reported_until_finish_lands(self):
        """收尾时先记「未上报」再移出 active，心跳不会漏报这条任务。"""
        client = FakeClient()
        exe = Executor(self.config, client=client)
        self.addCleanup(exe.stop)
        seen: list[list[str]] = []
        original = exe._finish

        def finish(job_id, outcome):
            # 收尾刚进入上报时（终态还没发出），心跳会报告哪些任务。
            with exe._lock:
                seen.append(sorted(exe._active_jobs | exe._unreported))
            return original(job_id, outcome)

        with exe._lock:
            exe._active_jobs.add("job-gap")
        with self._prepared(exe, clone=GitError("x")), \
                mock.patch.object(exe, "_finish", side_effect=finish):
            exe._run_job(self._job("job-gap"))
        self.assertEqual(seen, [["job-gap"]])
        self.assertNotIn("job-gap", exe._unreported)


if __name__ == "__main__":
    unittest.main()
