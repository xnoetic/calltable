"""0.3 预计时间：中位数估算、样本不足未知、阻塞者求和、ahead 明细。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from calltable import cli
from calltable.db import Store
from calltable.dashboard import PAGE
from calltable.hub import Hub
from tests.helpers import make_config


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "hub.sqlite3", estimate_window=20,
                           estimate_min_samples=3)
        for name in ("h1", "h2"):
            self.store.seed_host(name, {"light": 1, "heavy": 1}, [])
            self.store.heartbeat(name, "seed", [])
        self._seq = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ helpers
    def _job(self, **kw):
        self._seq += 1
        defaults = dict(project="demo", host="h1", slot="light", locks=[],
                        cmd=["true"], profile="demo@h1",
                        label=f"job{self._seq}")
        defaults.update(kw)
        return self.store.create_job(**defaults)

    def _finish_with_duration(self, job_id: str, duration: float,
                              status: str = "succeeded") -> None:
        """直接写时间戳，避免用 sleep 凑时序。"""
        self._clock = getattr(self, "_clock", 1000.0) + 10000
        self.store.finish(job_id, status=status, exit_code=0)
        finished = self._clock
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE jobs SET started=?, finished=? WHERE id=?",
                (finished - duration, finished, job_id))
        self.store._estimate_cache.clear()

    def _history(self, durations: list[float], **kw) -> None:
        for duration in durations:
            job = self._job(**kw)
            self._finish_with_duration(job["id"], duration)

    # ------------------------------------------------------------ 中位数
    def test_median_of_recent_successful_runs(self):
        self._history([10, 20, 30, 1000])
        job = self._job()
        estimate = self.store.duration_estimate(job)
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["seconds"], 25)  # median(10,20,30,1000)
        self.assertEqual(estimate["samples"], 4)
        self.assertEqual(estimate["basis"], "demo@h1 / light")

    def test_median_is_the_middle_value(self):
        self._history([10, 20, 30])
        estimate = self.store.duration_estimate(self._job())
        self.assertEqual(estimate["seconds"], 20)
        self.assertEqual(estimate["samples"], 3)

    def test_window_only_uses_the_most_recent_runs(self):
        self.store.estimate_window = 3
        self.store.estimate_min_samples = 3
        self._history([1000, 1000, 10, 20, 30])
        estimate = self.store.duration_estimate(self._job())
        self.assertEqual(estimate["seconds"], 20)

    def test_insufficient_samples_are_unknown(self):
        self._history([10, 20])
        self.assertIsNone(self.store.duration_estimate(self._job()))

    def test_failed_jobs_are_not_samples(self):
        for _ in range(5):
            job = self._job()
            self._finish_with_duration(job["id"], 99, status="failed")
        self.assertIsNone(self.store.duration_estimate(self._job()))

    def test_estimates_are_keyed_by_profile_and_slot(self):
        self._history([30, 30, 30], profile="demo@h1", slot="light")
        other_profile = self._job(profile="demo@h2", slot="light")
        other_slot = self._job(profile="demo@h1", slot="heavy")
        self.assertIsNone(self.store.duration_estimate(other_profile))
        self.assertIsNone(self.store.duration_estimate(other_slot))

    def test_shared_wildcard_profile_is_sampled_per_project(self):
        # *@h1 / *@* 被多个项目共用：样本仍按项目分开，否则 A 的快任务和
        # B 的慢任务会混进同一个中位数。
        self._history([10, 10, 10], project="fast", profile="*@h1")
        self._history([500, 500, 500], project="slow", profile="*@h1")
        fast = self.store.duration_estimate(self._job(project="fast", profile="*@h1"))
        slow = self.store.duration_estimate(self._job(project="slow", profile="*@h1"))
        self.assertEqual((fast["seconds"], fast["samples"]), (10, 3))
        self.assertEqual((slow["seconds"], slow["samples"]), (500, 3))

    def test_projects_without_profiles_fall_back_to_project_and_slot(self):
        self._history([12, 12, 12], profile="")
        no_profile = self._job(profile="")
        self.assertEqual(self.store.duration_estimate(no_profile)["seconds"], 12)

    # ------------------------------------------------------------ 队列
    def test_ahead_ids_lists_every_competing_earlier_job(self):
        first = self._job()
        second = self._job()
        other = self._job(host="h2")
        info = self.store.queue_info()
        self.assertEqual(info[first["id"]]["ahead"], 0)
        self.assertEqual(info[second["id"]]["ahead"], 1)
        self.assertEqual(info[second["id"]]["ahead_ids"], [first["id"]])
        self.assertEqual(info[other["id"]]["ahead"], 0)

    def test_wait_is_sum_of_queued_blockers(self):
        self._history([60, 60, 60])
        first = self._job()
        second = self._job()
        third = self._job()
        estimates = self.store.queue_estimates()
        self.assertEqual(estimates[first["id"]]["wait_seconds"], 0)
        self.assertEqual(estimates[first["id"]]["runtime_seconds"], 60)
        self.assertEqual(estimates[second["id"]]["wait_seconds"], 60)
        self.assertEqual(estimates[third["id"]]["wait_seconds"], 120)
        self.assertTrue(estimates[third["id"]]["known"])

    def test_running_blocker_contributes_remaining_time(self):
        self._history([100, 100, 100])
        running = self._job()
        self.assertEqual(self.store.claim("h1", "w1")["id"], running["id"])
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE jobs SET started=? WHERE id=?",
                (time.time() - 40, running["id"]))
        waiting = self._job()
        estimate = self.store.queue_estimates()[waiting["id"]]
        # 100s 预计 - 40s 已跑 = 60s 剩余
        self.assertGreaterEqual(estimate["wait_seconds"], 55)
        self.assertLessEqual(estimate["wait_seconds"], 62)

    def test_any_unknown_blocker_makes_the_wait_unknown(self):
        self._history([30, 30, 30])
        first = self._job()
        unknown = self._job(profile="demo@other", slot="light")
        third = self._job()
        estimates = self.store.queue_estimates()
        self.assertEqual(estimates[first["id"]]["wait_seconds"], 0)
        self.assertIsNone(estimates[third["id"]]["wait_seconds"])
        self.assertEqual(estimates[third["id"]]["wait_unknown_blockers"], 1)
        self.assertFalse(estimates[third["id"]]["known"])
        self.assertEqual(estimates[third["id"]]["runtime_seconds"], 30)

    def test_cli_and_dashboard_show_position_and_estimates(self):
        job = {
            "id": "job-1", "project": "demo", "host": "h1", "slot": "light",
            "status": "queued", "started": None, "queued_seconds": 90,
            "ahead": 1, "group_name": "linux", "runtime_seconds": None,
            "queue": {"ahead": 1, "queued_seconds": 90,
                      "wait_seconds": 120, "runtime_seconds": 300,
                      "runtime_samples": 5, "runtime_basis": "demo@h1 / light",
                      "known": True, "wait_unknown_blockers": 0},
        }
        line = cli._job_line(job)
        self.assertIn("1 ahead", line)
        self.assertIn("est wait 2m00s / run 5m00s", line)
        lines = " " .join(cli._estimate_lines(job))
        self.assertIn("1 ahead of you on linux", lines)
        self.assertIn("median of 5 recent succeeded run(s)", lines)
        self.assertIn("wait 2m00s", lines)

        unknown = dict(job, queue={"ahead": 1, "queued_seconds": 5,
                                  "wait_seconds": None, "runtime_seconds": None,
                                  "runtime_samples": 1,
                                  "wait_unknown_blockers": 1, "known": False})
        self.assertIn("est unknown", cli._job_line(unknown))
        unknown_lines = " " .join(cli._estimate_lines(unknown))
        self.assertIn("estimate: unknown", unknown_lines)
        self.assertNotIn("wait 0s", unknown_lines)
        self.assertNotIn("median of", unknown_lines)

        # 看板模板确实渲染预计时间与 webhook 状态
        self.assertIn("预计等待", PAGE)
        self.assertIn("预计耗时", PAGE)
        self.assertIn("renderWebhooks", PAGE)

    def test_hub_enriches_every_queued_job(self):
        profiles = self.root / "profiles"
        profiles.mkdir(exist_ok=True)
        (profiles / "demo@h1.json").write_text(json.dumps({
            "name": "demo@h1", "slot": "light"}), encoding="utf-8")
        config = make_config(self.root / "hub", hosts={
            "h1": {"slots": {"light": 1}}})
        hub = Hub(config)
        try:
            hub.create_job({"project": "demo", "host": "h1", "cmd": ["true"]})
            second = hub.create_job({"project": "demo", "host": "h1",
                                     "cmd": ["true"]})
            enriched = hub.with_queue_info([second])[0]
            self.assertEqual(enriched["ahead"], 1)
            self.assertIn("queue", enriched)
            self.assertEqual(enriched["queue"]["targets"], ["h1"])
            self.assertIsNone(enriched["queue"]["wait_seconds"])
            self.assertFalse(enriched["queue"]["known"])
        finally:
            hub.close()
            hub.store.close()


if __name__ == "__main__":
    unittest.main()
