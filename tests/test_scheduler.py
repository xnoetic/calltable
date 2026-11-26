"""调度原子性 + 执行器对账（B1/F1）+ 主机槽位语义（I1/I3）。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from calltable.db import (ALREADY_ATTACHED, MISSING_STRIKES, NOT_REQUEUED,
                          PENDING_WORKER_PREFIX, REQUEUED, Store)
from calltable.hub import Hub
from tests.helpers import make_config


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = self.root / "hub.sqlite3"
        self.store = Store(self.db)
        self._extra_stores = []
        for name, slots in (("h1", {"heavy": 1, "light": 2}),
                            ("h2", {"heavy": 1, "light": 1}),
                            ("win", {"heavy": 1, "light": 2})):
            self.store.seed_host(name, slots, [])
            # claim 只派给在线主机；测试里先打一次心跳。
            self.store.heartbeat(name, "seed", [])
        self._seq = 0

    def tearDown(self):
        for store in self._extra_stores:
            store.close()
        self.store.close()
        self.tmp.cleanup()

    def _job(self, **kw):
        self._seq += 1
        defaults = dict(project="p", host="h1", slot="light", locks=[],
                        cmd=["true"], submitter="tester", label=f"job{self._seq}")
        defaults.update(kw)
        return self.store.create_job(**defaults)

    # ------------------------------------------------------------ 槽位
    def test_slot_limit_is_per_host(self):
        a = self._job(slot="heavy")
        self.assertEqual(self.store.claim("h1", "w1")["id"], a["id"])
        self._job(slot="heavy")
        self.assertIsNone(self.store.claim("h1", "w1"))
        b = self._job(host="h2", slot="heavy")
        self.assertEqual(self.store.claim("h2", "w2")["id"], b["id"])

    def test_slot_stays_used_until_finish(self):
        first = self._job(slot="heavy")
        self.assertEqual(self.store.claim("h1", "w1")["id"], first["id"])
        second = self._job(slot="heavy")
        self.assertIsNone(self.store.claim("h1", "w1"))
        self.store.finish(first["id"], status="succeeded", exit_code=0)
        self.assertEqual(self.store.claim("h1", "w1")["id"], second["id"])

    def test_unconfigured_or_empty_slots_mean_zero(self):
        for name, slots in (("narrow", {"light": 1}), ("empty", {}),
                            ("nada", {})):
            self.store.seed_host(name, slots, [])
            self.store.heartbeat(name, "seed", [])
        self._job(host="narrow", slot="heavy")
        self.assertIsNone(self.store.claim("narrow", "w"))
        self._job(host="empty", slot="light")
        self.assertIsNone(self.store.claim("empty", "w"))
        # 未配置 slot 也不能靠「没上限」混过去
        self._job(host="narrow", slot="light")
        self.assertIsNotNone(self.store.claim("narrow", "w"))

    def test_host_isolation(self):
        self._job(host="win")
        self.assertIsNone(self.store.claim("h1", "w"))
        self.assertIsNotNone(self.store.claim("win", "w"))

    # ------------------------------------------------------------ 锁
    def test_named_lock_is_global_across_hosts(self):
        a = self._job(host="h1", locks=["gate-pg"])
        b = self._job(host="h2", locks=["gate-pg"])
        c = self._job(host="win", locks=["gate-pg"])
        self.assertEqual(self.store.claim("h1", "w1")["id"], a["id"])
        self.assertIsNone(self.store.claim("h2", "w2"))
        self.assertIsNone(self.store.claim("win", "w3"))
        self.store.finish(a["id"], status="succeeded", exit_code=0)
        self.assertEqual(self.store.claim("h2", "w2")["id"], b["id"])
        self.assertIsNone(self.store.claim("win", "w3"))
        self.store.finish(b["id"], status="succeeded", exit_code=0)
        self.assertEqual(self.store.claim("win", "w3")["id"], c["id"])

    def test_partial_lock_overlap_blocks(self):
        a = self._job(slot="light", locks=["l1", "l2"])
        self._job(slot="light", locks=["l2", "l3"])
        self.store.claim("h1", "w1")
        self.assertIsNone(self.store.claim("h1", "w1"))
        self.store.finish(a["id"], status="succeeded", exit_code=0)
        self.assertIsNotNone(self.store.claim("h1", "w1"))

    def test_cancel_queued_job_is_terminal(self):
        job = self._job()
        self.store.request_cancel(job["id"])
        self.assertEqual(self.store.job(job["id"])["status"], "cancelled")
        self.assertIsNone(self.store.claim("h1", "w"))

    # ------------------------------------------------------------ 并发
    def test_concurrent_claims_never_oversubscribe(self):
        for _ in range(20):
            self._job(slot="heavy")
        stores = [Store(self.db) for _ in range(8)]
        self._extra_stores.extend(stores)
        results = []
        barrier = threading.Barrier(len(stores))

        def worker(store):
            barrier.wait()
            results.append(store.claim("h1", "w"))

        threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len([job for job in results if job]), 1)
        self.assertEqual(len(self.store.jobs(status="running")), 1)

    def test_concurrent_lock_claims_are_exclusive(self):
        for _ in range(10):
            self._job(slot="light", locks=["shared"])
        stores = [Store(self.db) for _ in range(6)]
        self._extra_stores.extend(stores)
        results = []
        barrier = threading.Barrier(len(stores))

        def worker(store):
            barrier.wait()
            results.append(store.claim("h1", "w"))

        threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len([job for job in results if job]), 1)

    # ------------------------------------------------------------ B1 对账
    def test_heartbeat_reaps_job_from_previous_worker(self):
        job = self._job()
        self.store.claim("h1", "worker-old")
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        self.store.heartbeat("h1", "worker-new", [])
        reaped = self.store.job(job["id"])
        self.assertEqual(reaped["status"], "error")
        self.assertIn("worker changed", reaped["error"])
        # 槽位与锁立即释放
        host = {h["name"]: h for h in self.store.hosts()}["h1"]
        self.assertEqual(host["used"], {})
        self.assertEqual(host["locks"], [])

    def test_heartbeat_keeps_job_when_reported(self):
        job = self._job()
        self.store.claim("h1", "w1")
        self.store.mark_running_for_reconcile()
        self.store.heartbeat("h1", "w1", [job["id"]])
        current = self.store.job(job["id"])
        self.assertEqual(current["status"], "running")
        self.assertFalse(current["needs_reconcile"])

    def test_heartbeat_reaps_job_missing_twice(self):
        job = self._job()
        self.store.claim("h1", "w1")
        self.store.heartbeat("h1", "w1", [])
        self.assertEqual(self.store.job(job["id"])["status"], "running")
        for _ in range(MISSING_STRIKES - 1):
            self.store.heartbeat("h1", "w1", [])
        reaped = self.store.job(job["id"])
        self.assertEqual(reaped["status"], "error")
        self.assertIn("missing", reaped["error"])

    def test_stale_host_heartbeat_reaps_after_15_minutes(self):
        job = self._job()
        self.store.claim("h1", "w1")
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE hosts SET last_seen=? WHERE name='h1'",
                (time.time() - 1000,))
        reaped = self.store.reap_stale_hosts(900)
        self.assertEqual([job["id"]], [r["id"] for r in reaped])
        self.assertEqual(self.store.job(job["id"])["status"], "error")

    def test_fresh_host_is_not_reaped(self):
        job = self._job()
        self.store.claim("h1", "w1")
        self.store.heartbeat("h1", "w1", [job["id"]])
        self.assertEqual(self.store.reap_stale_hosts(900), [])
        self.assertEqual(self.store.job(job["id"])["status"], "running")

    def test_hub_restart_marks_running_but_does_not_refresh_last_seen(self):
        job = self._job()
        self.store.claim("h1", "w1")
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE hosts SET last_seen=? WHERE name='h1'",
                (time.time() - 5000,))
        # 模拟 hub 重启：同一个库上新建 Hub
        config = make_config(self.root, hosts={
            "h1": {"slots": {"heavy": 1, "light": 2}}})
        hub = Hub(config, store=self.store)
        try:
            self.assertTrue(self.store.job(job["id"])["needs_reconcile"])
            host = self.store.host("h1")
            self.assertGreater(time.time() - host["last_seen"], 4000)  # 仍是旧值
            # 新执行器实例心跳：worker 变了 → 立刻回收
            hub.heartbeat("h1", "w2", [])
            self.assertEqual(self.store.job(job["id"])["status"], "error")
        finally:
            hub.close()

    # ------------------------------------------------------------ 主机组
    def _group_job(self, targets, **kw):
        self._seq += 1
        job = self.store.create_job(
            project="p", host="grp", slot=kw.pop("slot", "heavy"),
            locks=kw.pop("locks", []), cmd=["true"], group_name="grp",
            targets=targets, **kw)
        return job

    def test_group_prefers_first_member(self):
        job = self._group_job(["h1", "h2"])
        self.assertIsNone(self.store.claim("h2", "w2"))
        claimed = self.store.claim("h1", "w1")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["host"], "h1")

    def test_group_falls_back_when_first_member_is_busy(self):
        self._job(host="h1", slot="heavy")
        self.store.claim("h1", "w1")
        job = self._group_job(["h1", "h2"])
        claimed = self.store.claim("h2", "w2")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["host"], "h2")

    def test_group_falls_back_when_first_member_is_offline(self):
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE hosts SET last_seen=? WHERE name='h1'",
                (time.time() - 1000,))
        job = self._group_job(["h1", "h2"])
        self.assertIsNone(self.store.claim("h1", "w1"))
        claimed = self.store.claim("h2", "w2")
        self.assertEqual(claimed["id"], job["id"])

    def test_group_job_is_claimed_by_exactly_one_host(self):
        job = self._group_job(["h1", "h2"])
        stores = [Store(self.db) for _ in range(6)]
        self._extra_stores.extend(stores)
        results = []
        barrier = threading.Barrier(len(stores))

        def worker(index, store):
            barrier.wait()
            results.append(store.claim("h1" if index % 2 == 0 else "h2", f"w{index}"))

        threads = [threading.Thread(target=worker, args=(i, store))
                   for i, store in enumerate(stores)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [job for job in results if job]
        self.assertEqual(len(claimed), 1, claimed)
        self.assertEqual(claimed[0]["id"], job["id"])

    # ------------------------------------------------------------ 按主机锁
    def test_host_scoped_lock_is_per_host(self):
        template = ["fullgate@{host}"]
        a = self.store.create_job(project="p", host="h1", slot="heavy",
                                  locks=template, cmd=["true"])
        b = self.store.create_job(project="p", host="h2", slot="heavy",
                                  locks=template, cmd=["true"])
        claimed_a = self.store.claim("h1", "w1")
        claimed_b = self.store.claim("h2", "w2")
        self.assertEqual(claimed_a["id"], a["id"])
        self.assertEqual(claimed_b["id"], b["id"])
        self.assertEqual(claimed_a["locks"], ["fullgate@h1"])
        self.assertEqual(claimed_b["locks"], ["fullgate@h2"])

    def test_host_scoped_lock_blocks_same_host_only(self):
        template = ["fullgate@{host}"]
        self.store.create_job(project="p", host="h1", slot="heavy",
                              locks=template, cmd=["true"])
        same = self.store.create_job(project="p", host="h1", slot="light",
                                     locks=template, cmd=["true"])
        other = self.store.create_job(project="p", host="h2", slot="light",
                                      locks=template, cmd=["true"])
        self.store.claim("h1", "w1")
        self.assertIsNone(self.store.claim("h1", "w1"))
        self.assertEqual(self.store.claim("h2", "w2")["id"], other["id"])
        self.assertEqual(self.store.job(same["id"])["status"], "queued")

    def test_global_lock_still_crosses_hosts(self):
        a = self.store.create_job(project="p", host="h1", slot="light",
                                  locks=["shared"], cmd=["true"])
        b = self.store.create_job(project="p", host="h2", slot="light",
                                  locks=["shared"], cmd=["true"])
        self.store.claim("h1", "w1")
        self.assertIsNone(self.store.claim("h2", "w2"))
        self.store.finish(a["id"], status="succeeded", exit_code=0)
        self.assertEqual(self.store.claim("h2", "w2")["id"], b["id"])

    # ------------------------------------------------------------ 队列可见性
    def test_queue_info_counts_earlier_jobs_competing_for_the_same_slot(self):
        first = self._job(slot="heavy", host="h1")
        second = self._job(slot="heavy", host="h1")
        other = self._job(slot="light", host="h1")
        info = self.store.queue_info()
        self.assertEqual(info[first["id"]]["ahead"], 0)
        self.assertEqual(info[second["id"]]["ahead"], 1)
        self.assertEqual(info[other["id"]]["ahead"], 0)
        self.assertGreaterEqual(info[first["id"]]["queued_seconds"], 0)

    def test_queue_info_counts_lock_contention_across_slots(self):
        blocker = self._job(slot="heavy", host="h1", locks=["fullgate@{host}"])
        waiter = self._job(slot="light", host="h1", locks=["fullgate@{host}"])
        unrelated = self._job(slot="light", host="h2")
        info = self.store.queue_info()
        self.assertEqual(info[waiter["id"]]["ahead"], 1)
        self.assertEqual(info[unrelated["id"]]["ahead"], 0)
        self.assertEqual(info[blocker["id"]]["ahead"], 0)

    def test_queue_info_ignores_jobs_that_target_other_hosts(self):
        self._job(slot="heavy", host="h2")
        mine = self._job(slot="heavy", host="h1")
        self.assertEqual(self.store.queue_info()[mine["id"]]["ahead"], 0)

    def test_disk_low_host_is_skipped_by_the_scheduler(self):
        job = self._job(slot="heavy", host="h1")
        self.store.heartbeat("h1", "w1", [],
                             system={"disk_low": True,
                                     "disk_reason": "runs over watermark"})
        self.assertIsNone(self.store.claim("h1", "w1"))
        self.assertEqual(self.store.job(job["id"])["status"], "queued")
        host = {h["name"]: h for h in self.store.hosts()}["h1"]
        self.assertTrue(host["disk_low"])
        self.assertEqual(host["system"]["disk_reason"],
                         "runs over watermark")
        self.store.heartbeat("h1", "w1", [], system={"disk_low": False})
        self.assertIsNotNone(self.store.claim("h1", "w1"))

    # ------------------------------------------------------------ drain
    def test_draining_host_is_skipped_until_undrained(self):
        job = self._job(slot="light", host="h1")
        host = self.store.set_draining("h1", True)
        self.assertTrue(host["draining"])
        self.assertIsNone(self.store.claim("h1", "w1"))
        self.assertEqual(self.store.job(job["id"])["status"], "queued")
        self.assertTrue({h["name"]: h for h in self.store.hosts()}["h1"]["draining"])
        self.store.set_draining("h1", False)
        self.assertIsNotNone(self.store.claim("h1", "w1"))

    def test_draining_first_group_member_falls_back(self):
        self.store.set_draining("h1", True)
        job = self._group_job(["h1", "h2"])
        self.assertIsNone(self.store.claim("h1", "w1"))
        claimed = self.store.claim("h2", "w2")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["host"], "h2")

    def test_set_draining_unknown_host_returns_none(self):
        self.assertIsNone(self.store.set_draining("nope", True))

    # ------------------------------------------------------------ 公平性
    def test_heavy_jobs_without_locks_run_in_parallel_on_one_host(self):
        self.store.seed_host("wide", {"heavy": 2, "light": 1}, [])
        self.store.heartbeat("wide", "seed", [])
        first = self.create_job_heavy("demo", "wide")
        second = self.create_job_heavy("other", "wide")
        claimed = self.store.claim("wide", "w1")
        self.assertEqual(claimed["id"], first["id"])
        claimed2 = self.store.claim("wide", "w1")
        self.assertEqual(claimed2["id"], second["id"])
        self.assertEqual(claimed["locks"], [])
        self.assertEqual(claimed2["locks"], [])

    def create_job_heavy(self, project, host):
        self._seq += 1
        return self.store.create_job(
            project=project, host=host, slot="heavy", locks=[], cmd=["true"],
            label=f"{project}-{self._seq}")

    def test_disk_low_group_member_falls_back_to_the_next_host(self):
        self.store.heartbeat("h1", "w1", [], system={"disk_low": True})
        job = self._group_job(["h1", "h2"])
        claimed = self.store.claim("h2", "w2")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["host"], "h2")

    # ------------------------------------------------------------ I3
    def test_hosts_view_groups_running_jobs(self):
        a = self._job(slot="light", locks=["x"])
        b = self._job(slot="light", locks=["y"])
        self.store.claim("h1", "w1")
        self.store.claim("h1", "w1")
        hosts = {host["name"]: host for host in self.store.hosts()}
        self.assertEqual(len(hosts["h1"]["running"]), 2)
        self.assertEqual(hosts["h1"]["used"], {"light": 2})
        self.assertEqual(hosts["h1"]["locks"], ["x", "y"])
        self.assertEqual(hosts["h2"]["running"], [])


class RequeueTests(unittest.TestCase):
    """溢出组：provider 主机（这里叫 cr）没拉起来时把任务放回队列。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "hub.sqlite3")
        self.store.seed_host("h1", {"light": 1}, [])
        self.store.seed_host("cr", {"light": 2}, [])
        self.store.heartbeat("h1", "seed", [])
        self.store.touch_host("cr")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fill_h1(self) -> dict:
        busy = self.store.create_job(project="p", host="h1", slot="light",
                                     locks=[], cmd=["true"])
        self.assertEqual(self.store.claim("h1", "w1")["id"], busy["id"])
        return busy

    def _group_job(self, targets=("h1", "cr"), locks=("gate@{host}", "db")):
        return self.store.create_job(
            project="p", host="pool", slot="light", locks=list(locks),
            cmd=["true"], group_name="pool", targets=list(targets))

    def _reserve(self, job: dict) -> dict:
        claimed = self.store.claim("cr", f"{PENDING_WORKER_PREFIX}abc")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["locks"], ["gate@cr", "db"])
        return claimed

    def test_requeue_restores_the_pre_claim_state(self):
        busy = self._fill_h1()
        job = self._group_job()
        self._reserve(job)
        outcome, requeued = self.store.requeue_unattached(
            job["id"], "cr", note="cr: launch failed")
        self.assertEqual(outcome, REQUEUED)
        self.assertEqual(requeued["status"], "queued")
        self.assertEqual(requeued["host"], "pool")
        self.assertEqual(requeued["worker"], "")
        self.assertIsNone(requeued["started"])
        self.assertFalse(requeued["needs_reconcile"])
        self.assertEqual(requeued["targets"], ["h1"])
        self.assertEqual(requeued["locks"], ["gate@{host}", "db"])
        self.assertEqual(requeued["created"], job["created"])
        self.assertEqual(requeued["requeue_note"], "cr: launch failed")
        self.assertEqual(requeued["error"], "")
        self.assertNotIn("lock_templates", requeued)
        types = [event["type"] for event in self.store.events_after(0, job["id"])]
        self.assertEqual(types, ["job.queued", "job.started", "job.requeued"])
        # 专用主机有空位就领走，锁按它展开。
        self.store.finish(busy["id"], status="succeeded", exit_code=0)
        claimed = self.store.claim("h1", "w1")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["host"], "h1")
        self.assertEqual(claimed["locks"], ["gate@h1", "db"])

    def test_requeued_job_keeps_its_place_in_the_queue(self):
        busy = self._fill_h1()
        first = self._group_job()
        self._reserve(first)
        later = self.store.create_job(project="p", host="h1", slot="light",
                                      locks=[], cmd=["true"])
        self.store.requeue_unattached(first["id"], "cr", note="x")
        self.store.finish(busy["id"], status="succeeded", exit_code=0)
        self.assertEqual(self.store.claim("h1", "w1")["id"], first["id"])
        self.assertEqual(self.store.job(later["id"])["status"], "queued")

    def test_attached_job_is_left_alone(self):
        self._fill_h1()
        job = self._group_job()
        self._reserve(job)
        self.assertIsNotNone(self.store.attach_job(job["id"], "cr", "exec-1"))
        outcome, requeued = self.store.requeue_unattached(job["id"], "cr", note="x")
        self.assertEqual((outcome, requeued), (ALREADY_ATTACHED, None))
        current = self.store.job(job["id"])
        self.assertEqual((current["status"], current["host"], current["worker"]),
                         ("running", "cr", "exec-1"))

    def test_single_target_job_is_not_requeued(self):
        job = self.store.create_job(project="p", host="cr", slot="light",
                                    locks=[], cmd=["true"])
        self.assertEqual(self.store.claim("cr", "pending:abc")["id"], job["id"])
        outcome, _ = self.store.requeue_unattached(job["id"], "cr", note="x")
        self.assertEqual(outcome, NOT_REQUEUED)
        self.assertEqual(self.store.job(job["id"])["status"], "running")

    def test_cancel_requested_or_unrecorded_templates_are_not_requeued(self):
        self._fill_h1()
        job = self._group_job()
        self._reserve(job)
        self.store.request_cancel(job["id"])
        self.assertEqual(self.store.requeue_unattached(job["id"], "cr", note="x")[0],
                         NOT_REQUEUED)
        # 升级前 claim 的任务没有锁模板：还原不了，不放回队列。
        self.store.conn.execute(
            "UPDATE jobs SET cancel_requested=0, lock_templates='' WHERE id=?",
            (job["id"],))
        self.assertEqual(self.store.requeue_unattached(job["id"], "cr", note="x")[0],
                         NOT_REQUEUED)

    def test_requeue_retracts_the_started_webhook(self):
        self.store.webhook_urls = ["http://hooks.example/a"]
        self._fill_h1()
        job = self._group_job()
        self._reserve(job)

        def events():
            return sorted((row["event"], row["status"]) for row in
                          self.store.webhook_deliveries(job["id"]))

        # job.started 已经投递出去：撤掉它，补一条 job.requeued。
        self.store.conn.execute(
            "UPDATE webhook_deliveries SET status='delivered', attempts=1 "
            "WHERE job_id=? AND event='job.started'", (job["id"],))
        self.store.requeue_unattached(job["id"], "cr", note="x")
        self.assertEqual(events(), [("job.requeued", "pending")])
        # 真正开跑时 job.started 能再投一次。
        self.store.conn.execute("UPDATE jobs SET status='succeeded' "
                                "WHERE host='h1' AND status='running'")
        self.assertEqual(self.store.claim("h1", "w1")["id"], job["id"])
        self.assertEqual(events(), [("job.requeued", "pending"),
                                    ("job.started", "pending")])

    def test_unsent_started_webhook_is_dropped_without_a_requeued_one(self):
        self.store.webhook_urls = ["http://hooks.example/a"]
        self._fill_h1()
        job = self._group_job()
        self._reserve(job)
        self.store.requeue_unattached(job["id"], "cr", note="x")
        self.assertEqual(self.store.webhook_deliveries(job["id"]), [])

    def test_cooling_down_member_does_not_block_the_next_member(self):
        # cr 在线、有空位、排在前面：不冷却时 h1 领不到。
        job = self._group_job(targets=("cr", "h1"), locks=())
        self.assertIsNone(self.store.claim("h1", "w1"))
        self.store.set_host_cooldown("cr", time.time() + 300)
        self.assertIsNone(self.store.claim("cr", "pending:abc"))
        claimed = self.store.claim("h1", "w1")
        self.assertEqual(claimed["id"], job["id"])
        self.store.set_host_cooldown("cr", 0)
        self.assertEqual(self.store.host_cooldowns, {})


class HubSlotsTests(unittest.TestCase):
    def test_hub_refuses_dispatch_without_slots_and_warns(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = make_config(Path(tmp), hosts={"bare": {"slots": {}}})
            hub = Hub(config)
            try:
                job = hub.store.create_job(project="p", host="bare", slot="light",
                                           locks=[], cmd=["true"])
                self.assertIsNone(hub.claim("bare", "w1"))
                self.assertEqual(hub.store.job(job["id"])["status"], "queued")
            finally:
                hub.close()
                hub.store.close()


if __name__ == "__main__":
    unittest.main()
