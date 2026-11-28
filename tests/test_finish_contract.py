"""finish 契约：终态幂等、晚到结果、归属检查，以及 pending 清理信号。

对应 spec/calltable-protocol.md「4.1 Who may write a terminal state」。要点：

* 已经终结的任务不被覆盖：同结果 → ``duplicate``（200），不同结果 →
  ``conflict``（200，body 是库里原来的记录）；
* 取消优先照旧：已请求取消的任务写成 ``cancelled``，重放算 ``duplicate``；
* 排队中 / 别的主机 / 别的 claim worker → 409 ``finish_rejected: true``；
* 坏 status / 坏 exit_code / body 不是对象 → 400，任务不存在 → 404，两者也带
  标记；普通路由的 404 没有标记；
* 拒收不改任务任何字段，也不发事件。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from calltable import hub as hub_module
from calltable.db import PENDING_WORKER_PREFIX, REQUEUED
from calltable.hub import Hub, HubServer
from tests.helpers import make_config

TOKEN = "finish-token"


class FinishContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.config = make_config(cls.root, hosts={})
        cls.config.raw["token"] = TOKEN
        cls.config.token = TOKEN
        cls.hub = Hub(cls.config)
        # 每个用例用自己的主机名，免得 claim 领到别的用例留下的排队任务。
        cls._seq = 0
        cls.server = HubServer(("127.0.0.1", 0), hub_module.Handler)
        cls.server.hub = cls.hub
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.hub.close()
        cls.hub.store.close()
        cls.tmp.cleanup()

    def setUp(self):
        type(self)._seq += 1
        self.host = f"fh{self._seq}"
        self._host(self.host)

    # ------------------------------------------------------------ helpers
    def _host(self, name: str) -> str:
        """登记一台新主机（配置 + 新鲜心跳），返回名字。"""
        self.hub.config.raw["hosts"][name] = {"slots": {"light": 4, "heavy": 1}}
        self.hub.store.seed_host(name, {"light": 4, "heavy": 1}, [])
        self.hub.store.heartbeat(name, f"seed-{name}", [])
        return name

    def _queue(self, host: str | None = None) -> dict:
        return self.hub.store.create_job(
            project="demo", host=host or self.host, slot="light", locks=[],
            cmd=["true"], timeout=60)

    def _group_job(self, *hosts: str) -> dict:
        """组任务：``targets`` 是成员，锁模板按成员展开（provider 放回队列用）。"""
        return self.hub.store.create_job(
            project="demo", host="grp", slot="light", locks=["box@{host}"],
            cmd=["true"], timeout=60, group_name="grp", targets=list(hosts))

    def _claim(self, worker: str = "exec-1", host: str | None = None,
               job: dict | None = None) -> dict:
        """让 ``host`` 领走一条任务：默认先排一条（已排好的用 ``job`` 传进来）。"""
        queued = job if job is not None else self._queue(host)
        claimed = self.hub.store.claim(host or self.host, worker)
        self.assertIsNotNone(claimed, "claim returned no job")
        self.assertEqual(claimed["id"], queued["id"])
        return claimed

    def _request(self, path: str, *, body=None, method: str = "POST"):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": "application/json"})
        return self._open(request)

    @staticmethod
    def _open(request):
        """返回 (status, payload)；4xx 也当正常结果读，不抛出。"""
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"raw": raw.decode("utf-8", "replace")}
            return exc.code, payload

    def _finish(self, job_id: str, body: dict):
        return self._request(f"/api/exec/jobs/{job_id}/finish", body=body)

    def _job(self, job_id: str) -> dict:
        job = self.hub.store.job(job_id)
        self.assertIsNotNone(job)
        return job

    def _finished_events(self, job_id: str) -> int:
        events = self.hub.store.events_after(0, job_id, limit=200)
        return sum(1 for event in events if event["type"] == "job.finished")

    def _set_worker(self, job_id: str, worker: str) -> None:
        with self.hub.store.lock:
            self.hub.store.conn.execute(
                "UPDATE jobs SET worker=? WHERE id=?", (worker, job_id))

    # ------------------------------------------------------------ 写入
    def test_first_report_is_recorded_and_answers_with_the_job(self):
        job = self._claim()
        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": self.host, "worker": "exec-1"})
        self.assertEqual(status, 200)
        self.assertIs(payload["finish_accepted"], True)
        self.assertEqual(payload["finish_reason"], "recorded")
        self.assertNotIn("finish_rejected", payload)
        self.assertEqual((payload["status"], payload["exit_code"]),
                         ("succeeded", 0))
        recorded = self._job(job["id"])
        self.assertEqual(recorded["status"], "succeeded")
        self.assertIsNotNone(recorded["finished"])
        self.assertEqual(self._finished_events(job["id"]), 1)

    def test_report_without_identity_is_accepted_like_0_2(self):
        # 老执行器只发 status / exit_code / error：不做归属判定（兼容优先）。
        job = self._claim()
        status, payload = self._finish(job["id"],
                                      {"status": "failed", "exit_code": 3})
        self.assertEqual(status, 200)
        self.assertEqual(payload["finish_reason"], "recorded")
        self.assertEqual(self._job(job["id"])["exit_code"], 3)

    # ------------------------------------------------------------ 幂等
    def test_repeating_the_same_result_is_a_duplicate_and_changes_nothing(self):
        job = self._claim()
        body = {"status": "succeeded", "exit_code": 0,
                "host": self.host, "worker": "exec-1"}
        self.assertEqual(self._finish(job["id"], body)[0], 200)
        first = self._job(job["id"])

        status, payload = self._finish(job["id"], body)
        self.assertEqual(status, 200)
        self.assertIs(payload["finish_accepted"], False)
        self.assertEqual(payload["finish_reason"], "duplicate")
        self.assertNotIn("finish_rejected", payload)
        # 回的是库里那条记录，不是这一条上报。
        self.assertEqual(payload["status"], "succeeded")
        second = self._job(job["id"])
        self.assertEqual(second["finished"], first["finished"])
        self.assertEqual(second["cleanup_at"], first["cleanup_at"])
        self.assertEqual(self._finished_events(job["id"]), 1)

    def test_a_different_late_result_does_not_overwrite_the_recorded_one(self):
        job = self._claim()
        self.assertEqual(self._finish(job["id"], {
            "status": "error", "exit_code": -1, "error": "boom",
            "host": self.host, "worker": "exec-1"})[0], 200)
        first = self._job(job["id"])

        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0})
        self.assertEqual(status, 200)
        self.assertIs(payload["finish_accepted"], False)
        self.assertEqual(payload["finish_reason"], "conflict")
        self.assertNotIn("finish_rejected", payload)
        self.assertEqual((payload["status"], payload["exit_code"]),
                         ("error", -1))
        self.assertEqual(payload["error"], "boom")
        second = self._job(job["id"])
        self.assertEqual((second["status"], second["exit_code"]),
                         ("error", -1))
        self.assertEqual(second["error"], "boom")
        self.assertEqual(second["finished"], first["finished"])
        self.assertEqual(second["cleanup_at"], first["cleanup_at"])
        self.assertEqual(self._finished_events(job["id"]), 1)

    def test_same_status_with_a_played_note_is_still_a_duplicate(self):
        # 执行器可能给同一条终态补说明：只有 status / exit_code 才参与判定。
        job = self._claim()
        self._finish(job["id"], {"status": "error", "exit_code": -1,
                                 "error": "first"})
        status, payload = self._finish(job["id"], {
            "status": "error", "exit_code": -1, "error": "second"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["finish_reason"], "duplicate")
        self.assertEqual(self._job(job["id"])["error"], "first")

    # ------------------------------------------------------------ 取消优先
    def test_cancel_precedence_is_kept_and_a_replay_is_a_duplicate(self):
        job = self._claim()
        self.hub.store.request_cancel(job["id"])

        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": self.host, "worker": "exec-1"})
        self.assertEqual(status, 200)
        self.assertIs(payload["finish_accepted"], True)
        self.assertEqual(payload["finish_reason"], "cancel_precedence")
        recorded = self._job(job["id"])
        # 取消优先只改状态和说明；报上来的真实退出码 0 保留（只有 None / -1
        # 这类「没有真实退出码」的情况才补成 -2）。
        self.assertEqual((recorded["status"], recorded["exit_code"]),
                         ("cancelled", 0))
        self.assertIn("cancel requested; reported succeeded", recorded["error"])

        # 同一条上报重放：折算之后还是同一个结果，不该报 conflict。
        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": self.host, "worker": "exec-1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["finish_reason"], "duplicate")
        self.assertEqual(self._job(job["id"])["status"], "cancelled")
        self.assertEqual(self._finished_events(job["id"]), 1)

    # ------------------------------------------------------------ 拒收
    def test_queued_job_rejects_a_late_result_with_409(self):
        job = self._queue()  # 从未 claim：只有执行器认领过的任务才能报终态
        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": self.host, "worker": "exec-1"})
        self.assertEqual(status, 409)
        self.assertIs(payload["finish_rejected"], True)
        self.assertEqual(payload["finish_reason"], "not_running")
        self.assertIn("error", payload)
        unchanged = self._job(job["id"])
        self.assertEqual(unchanged["status"], "queued")
        self.assertIsNone(unchanged["finished"])
        self.assertIsNone(unchanged["cleanup_at"])

    def test_requeued_group_job_rejects_the_old_execution(self):
        # provider 主机拉起失败 → 任务被放回队列，旧容器的结果不能再写进去。
        other = self._host(f"{self.host}-peer")
        job = self._group_job(self.host, other)
        claimed = self._claim(host=self.host, job=job)
        self.assertEqual(claimed["id"], job["id"])
        # 预留占位还在（执行器没 attach）时才可能被放回队列。
        self._set_worker(job["id"], PENDING_WORKER_PREFIX + "1")
        outcome, requeued = self.hub.store.requeue_unattached(
            job["id"], self.host, note="provider launch failed")
        self.assertEqual(outcome, REQUEUED)
        self.assertEqual(requeued["status"], "queued")

        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": self.host, "worker": "exec-1"})
        self.assertEqual(status, 409)
        self.assertIs(payload["finish_rejected"], True)
        self.assertIn(payload["finish_reason"], ("not_running", "other_owner"))
        unchanged = self._job(job["id"])
        self.assertEqual(unchanged["status"], "queued")
        self.assertIsNone(unchanged["finished"])

    def test_result_from_another_host_is_rejected_with_409(self):
        other = self._host(f"{self.host}-other")
        job = self._claim()
        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": other, "worker": "exec-1"})
        self.assertEqual(status, 409)
        self.assertIs(payload["finish_rejected"], True)
        self.assertEqual(payload["finish_reason"], "other_owner")
        unchanged = self._job(job["id"])
        self.assertEqual(unchanged["status"], "running")
        self.assertEqual(unchanged["host"], self.host)
        self.assertIsNone(unchanged["finished"])

    def test_result_from_another_claim_worker_is_rejected_with_409(self):
        job = self._claim(worker="exec-1")
        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": self.host, "worker": "exec-2"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["finish_reason"], "other_owner")
        self.assertEqual(self._job(job["id"])["status"], "running")

        # 原 claim worker 自己报就正常写入。
        status, payload = self._finish(job["id"], {
            "status": "succeeded", "exit_code": 0,
            "host": self.host, "worker": "exec-1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["finish_reason"], "recorded")

    def test_provider_placeholder_worker_is_not_an_owner_mismatch(self):
        # provider 预留（worker=pending:*）时执行器还没 attach：报上来的
        # worker 就是 attach 的那个，不能拿占位值去比。
        job = self._claim()
        self._set_worker(job["id"], PENDING_WORKER_PREFIX + "abc")
        status, payload = self._finish(job["id"], {
            "status": "failed", "exit_code": 1,
            "host": self.host, "worker": "exec-9"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["finish_reason"], "recorded")

    # ------------------------------------------------------------ 400
    def test_bad_terminal_status_is_rejected_with_400(self):
        job = self._claim()
        status, payload = self._finish(job["id"], {"status": "almost-done"})
        self.assertEqual(status, 400)
        self.assertIs(payload["finish_rejected"], True)
        self.assertEqual(payload["finish_reason"], "bad_request")
        self.assertEqual(self._job(job["id"])["status"], "running")

    def test_non_integer_or_boolean_exit_code_is_rejected_with_400(self):
        job = self._claim()
        for exit_code in (True, False, "0", 1.5, [0]):
            with self.subTest(exit_code=exit_code):
                status, payload = self._finish(job["id"], {
                    "status": "failed", "exit_code": exit_code})
                self.assertEqual(status, 400)
                self.assertIs(payload["finish_rejected"], True)
                self.assertEqual(payload["finish_reason"], "bad_request")
        unchanged = self._job(job["id"])
        self.assertEqual(unchanged["status"], "running")
        self.assertIsNone(unchanged["exit_code"])

    def test_missing_exit_code_is_allowed(self):
        # 结算类终态本来就可能没有真实退出码（准备阶段中止、对账）。
        job = self._claim()
        status, payload = self._finish(job["id"], {"status": "error"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["finish_reason"], "recorded")
        self.assertIsNone(self._job(job["id"])["exit_code"])

    def test_non_object_body_is_rejected_with_400(self):
        job = self._claim()
        for body in ([1, 2, 3], "succeeded", 7):
            with self.subTest(body=body):
                status, payload = self._finish(job["id"], body)
                self.assertEqual(status, 400)
                self.assertIs(payload["finish_rejected"], True)
                self.assertEqual(payload["finish_reason"], "bad_request")
        self.assertEqual(self._job(job["id"])["status"], "running")

    # ------------------------------------------------------------ 404
    def test_unknown_job_is_404_with_the_marker(self):
        status, payload = self._finish("job-does-not-exist", {
            "status": "succeeded", "exit_code": 0})
        self.assertEqual(status, 404)
        self.assertIs(payload["finish_rejected"], True)
        self.assertEqual(payload["finish_reason"], "unknown")

    def test_other_routes_do_not_carry_the_marker(self):
        # 普通 404 不能被当成「这条结果没用了」：执行器只认 finish 上的标记。
        status, payload = self._request(
            "/api/jobs/job-does-not-exist", method="GET")
        self.assertEqual(status, 404)
        self.assertNotIn("finish_rejected", payload)
        status, payload = self._request(
            "/api/jobs/job-does-not-exist/cancel", body={})
        self.assertEqual(status, 404)
        self.assertNotIn("finish_rejected", payload)


if __name__ == "__main__":
    unittest.main()
