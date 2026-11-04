"""日志分块 / 上限截断 / 过期清理（I6）。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from calltable.db import Store


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.job = self.store.create_job(
            project="p", host="h", slot="light", locks=[], cmd=["true"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_chunks_are_ordered_and_offsets_are_byte_exact(self):
        self.assertEqual(self.store.append_log(self.job["id"], b"hello "), (0, False))
        self.assertEqual(self.store.append_log(self.job["id"], b"world"), (6, False))
        data, nxt = self.store.read_logs(self.job["id"], 0)
        self.assertEqual(data, b"hello world")
        self.assertEqual(nxt, 11)
        data, nxt = self.store.read_logs(self.job["id"], 3)
        self.assertEqual(data, b"lo world")
        self.assertEqual(nxt, 11)
        data, nxt = self.store.read_logs(self.job["id"], 999)
        self.assertEqual(data, b"")
        self.assertEqual(nxt, 999)
        self.assertEqual(self.store.job(self.job["id"])["log_bytes"], 11)

    def test_multibyte_and_binary_safe(self):
        payload = "中文日志\n".encode("utf-8")
        self.store.append_log(self.job["id"], payload[:3])
        self.store.append_log(self.job["id"], payload[3:])
        data, _ = self.store.read_logs(self.job["id"], 0)
        self.assertEqual(data, payload)
        data, _ = self.store.read_logs(self.job["id"], 1)
        self.assertEqual(data, payload[1:])

    def test_empty_chunk_is_harmless(self):
        self.assertEqual(self.store.append_log(self.job["id"], b""), (0, False))
        data, nxt = self.store.read_logs(self.job["id"], 0)
        self.assertEqual(data, b"")
        self.assertEqual(nxt, 0)

    def test_log_cap_truncates_and_records_reason(self):
        store = Store(Path(self.tmp.name) / "cap.sqlite3", log_cap=10)
        try:
            job = store.create_job(project="p", host="h", slot="light",
                                   locks=[], cmd=["true"])
            self.assertEqual(store.append_log(job["id"], b"a" * 8), (0, False))
            start, truncated = store.append_log(job["id"], b"b" * 8)
            self.assertEqual(start, 8)
            self.assertTrue(truncated)
            self.assertEqual(store.read_logs(job["id"], 0)[0], b"a" * 8 + b"b" * 2)
            _, truncated_again = store.append_log(job["id"], b"c")
            self.assertTrue(truncated_again)
            self.assertIn("log truncated", store.job(job["id"])["error"])
        finally:
            store.close()


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_expired_job_purges_logs_samples_artifacts_and_bundle(self):
        job = self.store.create_job(project="p", host="h", slot="light",
                                    locks=[], cmd=["true"])
        self.store.finish(job["id"], status="succeeded", exit_code=0,
                          cleanup_at=time.time() - 10)
        self.store.append_log(job["id"], b"log")
        self.store.add_sample(job["id"], 1000, 10, 10)
        artifact = self.root / "art.bin"
        artifact.write_bytes(b"data")
        bundle = self.root / "b.bundle"
        bundle.write_bytes(b"bundle")
        self.store.add_artifact(job["id"], "art.bin", 4, "sha", str(artifact))
        self.store.add_bundle("bid", "p", str(bundle), 6)
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE jobs SET bundle_id='bid' WHERE id=?", (job["id"],))

        expired = self.store.expired_jobs(time.time())
        self.assertEqual([job["id"]], [row["id"] for row in expired])
        files = self.store.purge_job(job["id"])
        self.assertIn(str(artifact), files)
        self.assertIn(str(bundle), files)
        self.assertEqual(self.store.read_logs(job["id"], 0)[0], b"")
        self.assertEqual(self.store.samples(job["id"]), [])
        self.assertEqual(self.store.artifacts(job["id"]), [])
        self.assertIsNone(self.store.bundle("bid"))
        self.assertTrue(self.store.job(job["id"])["purged"])
        self.assertEqual(self.store.expired_jobs(time.time()), [])

    def test_orphan_bundles_are_expired(self):
        path = self.root / "orphan.bundle"
        path.write_bytes(b"x")
        self.store.add_bundle("oid", "p", str(path), 1)
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE bundles SET created=? WHERE id='oid'", (time.time() - 7200,))
        files = self.store.purge_orphan_bundles(time.time(), max_age=3600)
        self.assertEqual(files, [str(path)])
        self.assertIsNone(self.store.bundle("oid"))


if __name__ == "__main__":
    unittest.main()
