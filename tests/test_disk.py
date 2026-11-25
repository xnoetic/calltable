"""磁盘水位保护：清理最旧的已结束 run、保护运行中的、上报 disk_low。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from calltable import disk
from calltable.config import Config, ConfigError  # noqa: F401  (配置接口存在性)
from tests.helpers import make_config

GB = disk.GB


class DiskGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name) / "runs"
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, name: str, size_mb: int, finished_at: float | None = None,
             running: bool = False) -> Path:
        run_dir = self.root / name
        run_dir.mkdir()
        (run_dir / "work").mkdir()
        with open(run_dir / "work" / "blob.bin", "wb") as handle:
            handle.write(b"x" * (size_mb * 1024 * 1024))
        if finished_at is not None:
            (run_dir / disk.FINISHED_MARKER).write_text(str(finished_at))
        if running:
            (run_dir / ".running").write_text("worker-1")
        return run_dir

    def test_directory_size_counts_files(self):
        self._run("a", 2)
        size = disk.directory_size(self.root)
        self.assertGreaterEqual(size, 2 * 1024 * 1024 - 1024)

    def test_no_watermark_means_no_cleanup(self):
        self._run("a", 2, finished_at=time.time() - 100)
        status = disk.enforce(self.root, min_free_gb=0, runs_max_gb=0,
                              free_fn=lambda path: 10 * GB)
        self.assertFalse(status.low)
        self.assertEqual(status.reclaimed, 0)
        self.assertTrue((self.root / "a").exists())

    def test_low_free_space_removes_oldest_finished_runs_first(self):
        old = self._run("old", 2, finished_at=1000)
        middle = self._run("middle", 2, finished_at=2000)
        new = self._run("new", 2, finished_at=3000)
        free = {"bytes": 100 * 1024 * 1024}

        def free_fn(path):
            return free["bytes"]

        removed: list[str] = []
        real_rmtree = disk.shutil.rmtree

        def fake_rmtree(path, ignore_errors=False):
            removed.append(Path(path).name)
            real_rmtree(path, ignore_errors=ignore_errors)
            free["bytes"] += 2 * 1024 * 1024

        original = disk.shutil.rmtree
        disk.shutil.rmtree = fake_rmtree
        try:
            status = disk.enforce(self.root, min_free_gb=1, runs_max_gb=0,
                                  free_fn=free_fn)
        finally:
            disk.shutil.rmtree = original
        # 三份各补 1G，但缺口是 1G 以上，所以最后还是 disk_low。
        self.assertTrue(status.low)
        self.assertEqual(removed, ["old", "middle", "new"])
        self.assertFalse(old.exists() or middle.exists() or new.exists())

    def test_cleanup_stops_as_soon_as_watermark_is_met(self):
        old = self._run("old", 2, finished_at=1000)
        new = self._run("new", 2, finished_at=3000)
        free = {"bytes": 0}

        def free_fn(path):
            return free["bytes"]

        original = disk.shutil.rmtree

        def fake_rmtree(path, ignore_errors=False):
            original(path, ignore_errors=ignore_errors)
            free["bytes"] += GB

        disk.shutil.rmtree = fake_rmtree
        try:
            status = disk.enforce(self.root, min_free_gb=1, runs_max_gb=0,
                                  free_fn=free_fn)
        finally:
            disk.shutil.rmtree = original
        self.assertFalse(status.low)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_running_dirs_are_never_removed(self):
        self._run("live", 2, finished_at=1000, running=True)
        status = disk.enforce(self.root, min_free_gb=1, runs_max_gb=0,
                              free_fn=lambda path: 0,
                              protected={"live"})
        self.assertTrue(status.low)
        self.assertEqual(status.reclaimed, 0)
        self.assertTrue((self.root / "live").exists())

    def test_protected_dirs_are_kept_even_without_running_marker(self):
        self._run("active", 2, finished_at=1000)
        status = disk.enforce(self.root, min_free_gb=1, runs_max_gb=0,
                              free_fn=lambda path: 0,
                              protected={"active"})
        self.assertTrue(status.low)
        self.assertTrue((self.root / "active").exists())

    def test_runs_size_watermark_cleans_even_with_plenty_of_free_space(self):
        self._run("big", 3, finished_at=1000)
        status = disk.enforce(self.root, min_free_gb=0, runs_max_gb=0.001,
                              free_fn=lambda path: 500 * GB)
        self.assertFalse(status.low)
        self.assertEqual(status.reclaimed, 1)
        self.assertFalse((self.root / "big").exists())

    def test_undeletable_oldest_run_does_not_stall_cleanup(self):
        # lazyprod 实况：最旧的 run 里有容器以 root 写进来的 node_modules，
        # rmtree(ignore_errors=True) 静默失败，旧逻辑每轮都重新挑中它、死循环。
        stuck = self._run("stuck", 2, finished_at=1000)
        old = self._run("old", 2, finished_at=2000)
        new = self._run("new", 2, finished_at=3000)
        original = disk.shutil.rmtree
        attempts: list[str] = []

        def fake_rmtree(path, ignore_errors=False):
            attempts.append(Path(path).name)
            if len(attempts) > 20:
                raise AssertionError("enforce keeps retrying the same run dir")
            if Path(path).name == "stuck":
                return
            original(path, ignore_errors=ignore_errors)

        disk.shutil.rmtree = fake_rmtree
        try:
            status = disk.enforce(self.root, min_free_gb=0,
                                  runs_max_gb=4.5 / 1024,  # 4.5 MiB；现有 6 MiB
                                  free_fn=lambda path: 500 * GB, cache={})
        finally:
            disk.shutil.rmtree = original
        self.assertEqual(attempts.count("stuck"), 1)
        self.assertEqual(status.undeletable, ["stuck"])
        self.assertTrue(stuck.exists())
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(status.reclaimed, 1)
        self.assertFalse(status.low)
        self.assertEqual(status.to_dict()["disk_undeletable"], 1)

    def test_cleanup_accounts_victims_with_their_cached_size(self):
        runs = [self._run(f"r{index}", 2, finished_at=1000 + index)
                for index in range(3)]
        cache: dict = {}
        disk.runs_size(self.root, cache)
        original = disk.directory_size
        # 限时 walk 被截断时只量到一点点；旧逻辑拿它去扣总量，会一路多删。
        disk.directory_size = lambda path, deadline=None: 0
        try:
            status = disk.enforce(self.root, min_free_gb=0,
                                  runs_max_gb=4.5 / 1024,
                                  free_fn=lambda path: 500 * GB, cache=cache)
        finally:
            disk.directory_size = original
        self.assertEqual(status.reclaimed, 1)
        self.assertFalse(runs[0].exists())
        self.assertTrue(runs[1].exists())
        self.assertTrue(runs[2].exists())
        self.assertNotIn("r0", cache)

    def test_host_stats_reports_memory_load_and_disk(self):
        self._run("a", 1, finished_at=1000)
        payload = disk.host_stats(self.root, min_free_gb=0, runs_max_gb=0,
                                  enforce_watermarks=True)
        for key in ("disk_free_gb", "disk_runs_gb", "disk_low", "load1",
                    "mem_total_gb", "mem_free_gb"):
            self.assertIn(key, payload)
        self.assertFalse(payload["disk_low"])


class RunsSizeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name) / "runs"
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, name, size_mb, mtime=None):
        run_dir = self.root / name
        run_dir.mkdir()
        (run_dir / "f.bin").write_bytes(b"x" * (size_mb * 1024 * 1024))
        if mtime is not None:
            os.utime(run_dir, (mtime, mtime))
        return run_dir

    def test_cache_reuses_unchanged_directory_sizes(self):
        self._run("done", 1, mtime=1000)
        cache: dict = {}
        first, partial = disk.runs_size(self.root, cache, budget=5)
        self.assertFalse(partial)
        self.assertGreaterEqual(first, 1024 * 1024)
        self.assertIn("done", cache)
        second, _ = disk.runs_size(self.root, cache, budget=5)
        self.assertEqual(first, second)
        # 目录消失后缓存条目也要清掉
        __import__("shutil").rmtree(self.root / "done")
        third, _ = disk.runs_size(self.root, cache, budget=5)
        self.assertEqual(third, 0)
        self.assertEqual(cache, {})

    def test_scan_budget_marks_partial(self):
        for index in range(5):
            self._run(f"r{index}", 1, mtime=1000 + index)
        cache: dict = {}
        size, partial = disk.runs_size(self.root, cache, budget=0)
        self.assertTrue(partial)
        self.assertEqual(size, 0)

    def test_partial_scan_does_not_trigger_runs_max_gb(self):
        for index in range(3):
            self._run(f"r{index}", 1, mtime=1000 + index)
        status = disk.enforce(self.root, min_free_gb=0, runs_max_gb=0.0001,
                              free_fn=lambda path: 500 * GB, cache={},
                              scan_budget=0)
        self.assertTrue(status.runs_partial)
        self.assertFalse(status.low)


class ExecutorConfigTests(unittest.TestCase):
    def test_watermark_config_defaults_to_disabled(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = make_config(Path(tmp))
            self.assertEqual(config.min_free_gb, 0)
            self.assertEqual(config.runs_max_gb, 0)
            config = make_config(Path(tmp) / "b", min_free_gb=100,
                                 runs_max_gb=300)
            self.assertEqual(config.min_free_gb, 100)
            self.assertEqual(config.runs_max_gb, 300)

    def test_example_config_has_watermarks(self):
        import json

        path = Path(__file__).resolve().parents[1] / "config.example.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["min_free_gb"], 20)
        self.assertEqual(raw["runs_max_gb"], 100)
        self.assertEqual(raw["hosts"]["runner-b"]["slots"]["light"], 2)
        self.assertEqual(raw["hosts"]["runner-a"]["slots"]["heavy"], 2)


if __name__ == "__main__":
    unittest.main()
