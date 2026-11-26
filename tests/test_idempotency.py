"""0.3 幂等取号：同 key 同参数复用、参数不一致拒绝、窗口过期、HTTP 语义。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from calltable.db import IdempotencyConflict, Store
from calltable.hub import Handler, Hub, HubServer
from tests.helpers import make_config

TOKEN = "idem-token"


class IdempotencyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "hub.sqlite3")
        self.store.seed_host("h1", {"light": 2}, [])
        self.store.heartbeat("h1", "seed", [])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _create(self, key: str = "key-1", fingerprint: str = "fp-1", **kw):
        defaults = dict(project="demo", host="h1", slot="light", locks=[],
                        cmd=["true"], idempotency={
                            "key": key, "fingerprint": fingerprint,
                            "window": 3600})
        defaults.update(kw)
        return self.store.create_job(**defaults)

    def test_same_key_same_fingerprint_returns_the_same_job(self):
        first = self._create()
        second = self._create()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second.get("idempotent_replay"))
        self.assertFalse(first.get("idempotent_replay"))
        self.assertEqual(len(self.store.jobs(limit=10)), 1)
        # 没有多占队列位置
        self.assertEqual(self.store.queue_info()[first["id"]]["ahead"], 0)

    def test_same_key_different_fingerprint_conflicts(self):
        self._create()
        with self.assertRaises(IdempotencyConflict) as ctx:
            self._create(fingerprint="fp-2")
        self.assertIn("different", str(ctx.exception))
        self.assertIn("key-1", str(ctx.exception))
        self.assertEqual(len(self.store.jobs(limit=10)), 1)

    def test_expired_key_takes_a_new_ticket(self):
        first = self._create()
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE idempotency SET created=? WHERE key='key-1'",
                (time.time() - 7200,))
        second = self._create()
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second.get("idempotent_replay"))
        self.assertEqual(len(self.store.jobs(limit=10)), 2)

    def test_missing_job_row_frees_the_key(self):
        first = self._create()
        with self.store.lock:
            self.store.conn.execute("DELETE FROM jobs WHERE id=?",
                                    (first["id"],))
        second = self._create()
        self.assertNotEqual(first["id"], second["id"])

    def test_prune_idempotency_removes_expired_keys(self):
        self._create()
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE idempotency SET created=?", (time.time() - 100,))
        self.assertEqual(self.store.prune_idempotency(window=10), 1)


class IdempotencyHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.config = make_config(cls.root, hosts={
            "h1": {"slots": {"light": 2}}})
        cls.config.raw["token"] = TOKEN
        cls.config.token = TOKEN
        cls.hub = Hub(cls.config)
        cls.hub.store.seed_host("h1", {"light": 2}, [])
        cls.hub.store.heartbeat("h1", "seed", [])
        cls.server = HubServer(("127.0.0.1", 0), Handler)
        cls.server.hub = cls.hub
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.hub.close()
        cls.hub.store.close()
        cls.tmp.cleanup()

    def _post(self, body, key: str | None = None):
        headers = {"Authorization": f"Bearer {TOKEN}",
                   "Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/jobs",
            data=json.dumps(body).encode(), method="POST", headers=headers)
        return urllib.request.urlopen(request, timeout=10)

    @staticmethod
    def _body(label="idem"):
        return {"project": "demo", "host": "h1", "slot": "light",
                "cmd": ["true"], "label": label}

    def test_header_key_replays_with_200_and_no_duplicate(self):
        with self._post(self._body(), key="http-key") as resp:
            first = json.loads(resp.read())
            self.assertEqual(resp.status, 201)
            self.assertNotIn("X-Calltable-Idempotent-Replay", resp.headers)
        with self._post(self._body(), key="http-key") as resp:
            second = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("X-Calltable-Idempotent-Replay"),
                             "true")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(second["idempotency_key"], "http-key")

    def test_body_key_also_works(self):
        body = dict(self._body("body-key"), idempotency_key="body-key")
        with self._post(body) as resp:
            first = json.loads(resp.read())
        with self._post(body) as resp:
            second = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
        self.assertEqual(first["id"], second["id"])

    def test_conflict_returns_409_and_explains(self):
        with self._post(self._body("one"), key="conflict-key"):
            pass
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(self._body("two"), key="conflict-key")
        self.assertEqual(ctx.exception.code, 409)
        payload = json.loads(ctx.exception.read())
        self.assertIn("different", payload["error"])

    def test_bad_key_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(self._body(), key="bad key with spaces")
        self.assertEqual(ctx.exception.code, 400)

    def test_clean_tree_code_identity_is_the_commit_sha(self):
        body = dict(self._body("code-clean"), dirty=False, sha="1" * 40,
                    tree_sha="2" * 40)
        with self._post(body, key="code-clean-key") as resp:
            first = json.loads(resp.read())
            self.assertEqual(resp.status, 201)
        # 同样的内容（同 sha）重试 -> 复用
        with self._post(body, key="code-clean-key") as resp:
            second = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
        self.assertEqual(first["id"], second["id"])
        # 新的提交（sha 变了）-> 409，不能把旧结果当成新代码的结果
        changed = dict(body, sha="3" * 40)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(changed, key="code-clean-key")
        self.assertEqual(ctx.exception.code, 409)

    def test_dirty_tree_code_identity_is_the_tree_hash(self):
        dirty = dict(self._body("code-dirty"), dirty=True, sha="4" * 40,
                     tree_sha="5" * 40)
        with self._post(dirty, key="code-dirty-key") as resp:
            first = json.loads(resp.read())
            self.assertEqual(resp.status, 201)
        # 重新生成的快照 commit sha 不同，但 tree hash 相同 -> 复用
        regenerated = dict(dirty, sha="6" * 40)
        with self._post(regenerated, key="code-dirty-key") as resp:
            second = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
        self.assertEqual(first["id"], second["id"])
        # 内容变了，tree hash 变了 -> 409
        changed = dict(dirty, sha="7" * 40, tree_sha="8" * 40)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(changed, key="code-dirty-key")
        self.assertEqual(ctx.exception.code, 409)

    def test_malformed_tree_sha_is_rejected(self):
        body = dict(self._body("bad-tree"), dirty=True, sha="9" * 40,
                    tree_sha="not-a-hash")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(body, key="bad-tree-key")
        self.assertEqual(ctx.exception.code, 400)

    def test_response_includes_queue_and_capabilities(self):
        with self._post(self._body("receipt")) as resp:
            job = json.loads(resp.read())
        self.assertIn("queue", job)
        self.assertIn("ahead", job["queue"])
        self.assertIn("wait_seconds", job["queue"])
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/health", timeout=10) as resp:
            health = json.loads(resp.read())
        for capability in ("queue_eta", "wait_longpoll", "events_sse",
                           "webhooks", "idempotency", "queue_hold"):
            self.assertIn(capability, health["capabilities"])


LEGACY_SCHEMA = """
CREATE TABLE hosts (
    name TEXT PRIMARY KEY, os TEXT NOT NULL DEFAULT '',
    slots TEXT NOT NULL DEFAULT '{}', labels TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',
    last_seen REAL NOT NULL DEFAULT 0, worker TEXT NOT NULL DEFAULT '',
    heartbeat_jobs TEXT NOT NULL DEFAULT '[]',
    system TEXT NOT NULL DEFAULT '{}', draining INTEGER NOT NULL DEFAULT 0);
CREATE TABLE projects (name TEXT PRIMARY KEY, mirror TEXT NOT NULL,
    created REAL NOT NULL);
CREATE TABLE bundles (id TEXT PRIMARY KEY, project TEXT NOT NULL,
    path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, job_id TEXT NOT NULL DEFAULT '');
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, host TEXT NOT NULL,
    slot TEXT NOT NULL, locks TEXT NOT NULL DEFAULT '[]', cmd TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '', submitter TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
    sha TEXT NOT NULL DEFAULT '', base_ref TEXT NOT NULL DEFAULT '',
    base_ref_sha TEXT NOT NULL DEFAULT '', base_sha TEXT NOT NULL DEFAULT '',
    dirty INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
    created REAL NOT NULL, group_name TEXT NOT NULL DEFAULT '',
    targets TEXT NOT NULL DEFAULT '[]', started REAL, finished REAL,
    exit_code INTEGER, timeout INTEGER NOT NULL DEFAULT 10800,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker TEXT NOT NULL DEFAULT '', log_bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', cleanup_at REAL,
    cpu_total_sec REAL NOT NULL DEFAULT 0,
    cpu_peak_cores REAL NOT NULL DEFAULT 0,
    mem_peak INTEGER NOT NULL DEFAULT 0, bundle_id TEXT NOT NULL DEFAULT '',
    misses INTEGER NOT NULL DEFAULT 0,
    needs_reconcile INTEGER NOT NULL DEFAULT 0,
    purged INTEGER NOT NULL DEFAULT 0);
CREATE INDEX jobs_status ON jobs(status, created);
CREATE INDEX jobs_host ON jobs(host, status);
CREATE TABLE logs (job_id TEXT NOT NULL, start INTEGER NOT NULL,
    end INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY (job_id, start));
CREATE TABLE samples (job_id TEXT NOT NULL, ts REAL NOT NULL,
    cpu_usec INTEGER NOT NULL, mem_current INTEGER NOT NULL,
    mem_peak INTEGER NOT NULL);
CREATE INDEX samples_job ON samples(job_id, ts);
CREATE TABLE artifacts (job_id TEXT NOT NULL, name TEXT NOT NULL,
    size INTEGER NOT NULL, sha256 TEXT NOT NULL, stored TEXT NOT NULL,
    PRIMARY KEY (job_id, name));
"""


class MigrationTests(unittest.TestCase):
    """0.2 数据库启动新 hub：自动迁移、数据不丢、新功能可用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = self.root / "hub.sqlite3"
        conn = sqlite3.connect(str(self.db))
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO hosts(name, slots, last_seen) VALUES('h1', ?, ?)",
            (json.dumps({"light": 1}), time.time()))
        conn.execute(
            "INSERT INTO jobs(id, project, host, slot, locks, cmd, profile, "
            "submitter, label, status, created, started, finished, exit_code, "
            "timeout, targets) VALUES('job-old', 'demo', 'h1', 'light', '[]', "
            "'[\"true\"]', 'demo@h1', 'dev', 'old', 'succeeded', ?, ?, ?, 0, "
            "3600, '[\"h1\"]')",
            (time.time() - 100, time.time() - 90, time.time() - 60))
        conn.execute(
            "INSERT INTO logs(job_id, start, end, data) VALUES('job-old', 0, 5, ?)",
            (sqlite3.Binary(b"hello"),))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_database_is_migrated_without_losing_data(self):
        store = Store(self.db)
        try:
            job = store.job("job-old")
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(job["label"], "old")
            self.assertEqual(job["webhooks"], [])
            self.assertEqual(job["hold_seconds"], 0)
            self.assertEqual(job["idempotency_key"], "")
            self.assertEqual(store.read_logs("job-old")[0], b"hello")
            columns = {row["name"] for row in store.conn.execute(
                "PRAGMA table_info(jobs)")}
            for column in ("webhooks", "hold_seconds", "last_hold",
                           "idempotency_key"):
                self.assertIn(column, columns)
            tables = {row["name"] for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("events", "webhook_deliveries", "idempotency"):
                self.assertIn(table, table)
            # 迁移后的旧任务参与预计时间（1 个样本，低于阈值 → 未知）
            self.assertIsNone(store.duration_estimate(job))
            # 新任务照常取号
            new = store.create_job(project="demo", host="h1", slot="light",
                                   locks=[], cmd=["true"], profile="demo@h1")
            self.assertEqual(new["status"], "queued")
        finally:
            store.close()

    def test_overflow_columns_are_added_to_an_old_database(self):
        store = Store(self.db)
        try:
            columns = {row["name"] for row in store.conn.execute(
                "PRAGMA table_info(jobs)")}
            self.assertIn("lock_templates", columns)
            self.assertIn("requeue_note", columns)
            old = store.job("job-old")
            self.assertEqual(old["requeue_note"], "")
            self.assertNotIn("lock_templates", old)
            # 迁移后 claim 照常记下锁模板。
            store.heartbeat("h1", "w1", [])
            new = store.create_job(project="demo", host="h1", slot="light",
                                   locks=["gate@{host}"], cmd=["true"])
            self.assertEqual(store.claim("h1", "w1")["locks"], ["gate@h1"])
            row = store.conn.execute("SELECT lock_templates FROM jobs WHERE id=?",
                                     (new["id"],)).fetchone()
            self.assertEqual(json.loads(row["lock_templates"]), ["gate@{host}"])
        finally:
            store.close()

    def test_hub_starts_on_an_old_database(self):
        config = make_config(self.root / "cfg", hosts={
            "h1": {"slots": {"light": 1}}})
        config.raw["data_dir"] = str(self.root)
        config.data_dir = self.root
        config.raw["hub_port"] = 0
        hub = Hub(config)
        try:
            self.assertIsNotNone(hub.store.job("job-old"))
            job = hub.create_job({"project": "demo", "host": "h1",
                                  "cmd": ["true"]})
            receipt = hub.with_queue_info([job])[0]
            self.assertEqual(receipt["queue"]["ahead"], 0)
            self.assertIsNone(receipt["queue"]["runtime_seconds"])
        finally:
            hub.close()
            hub.store.close()


if __name__ == "__main__":
    unittest.main()
