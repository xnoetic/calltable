"""hub 层：bundle 登记与路径校验（B4）、项目名校验（I4）、槽位告警（I1）、
清理（I6）与一次批量 have 查询（I9）。"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from calltable import __version__, gitops
from calltable.hub import Hub
from calltable.profiles import load_profiles
from tests.helpers import make_config


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        profiles = self.root / "profiles"
        profiles.mkdir(parents=True, exist_ok=True)
        (profiles / "demo@h1.json").write_text(json.dumps({
            "name": "demo@h1", "slot": "light", "artifacts": ["out/*.json"],
            "timeout": "1m"}), encoding="utf-8")
        self.config = make_config(self.root, hosts={
            "h1": {"slots": {"heavy": 1, "light": 2}}})
        self.hub = Hub(self.config)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@e")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.head = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.hub.close()
        self.hub.store.close()
        self.tmp.cleanup()

    def _bundle(self) -> tuple[str, Path, int]:
        path = self.hub.bundles / "upload.bundle"
        gitops.create_bundle(self.repo, self.head, None, path)
        result = self.hub.register_bundle("demo", path, path.stat().st_size)
        return result["bundle_id"], path, path.stat().st_size

    # ------------------------------------------------------------ B4
    def test_registered_bundle_is_accepted(self):
        bundle_id, path, _ = self._bundle()
        resolved = self.hub.bundle_path(bundle_id)
        self.assertEqual(resolved, path.resolve())
        job = self.hub.create_job({
            "project": "demo", "host": "h1", "cmd": ["true"],
            "sha": self.head, "bundle_id": bundle_id})
        self.assertIsNotNone(self.hub.store.job_bundle(job["id"]))

    def test_unknown_bundle_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.hub.create_job({"project": "demo", "host": "h1",
                                 "cmd": ["true"], "bundle_id": "deadbeef"})

    def test_bundle_path_outside_root_is_rejected(self):
        outside = self.root / "evil.bundle"
        outside.write_bytes(b"nope")
        with self.hub.store.lock:
            self.hub.store.conn.execute(
                "INSERT INTO bundles(id, project, path, size, created) "
                "VALUES('evil','demo',?,1,?)", (str(outside), time.time()))
        self.assertIsNone(self.hub.bundle_path("evil"))
        with self.assertRaises(ValueError):
            self.hub.create_job({"project": "demo", "host": "h1", "cmd": ["true"],
                                 "bundle_id": "evil"})

    def test_bundle_from_another_project_is_rejected(self):
        bundle_id, _, _ = self._bundle()
        with self.assertRaises(ValueError):
            self.hub.create_job({"project": "other", "host": "h1",
                                 "cmd": ["true"], "bundle_id": bundle_id})

    # ------------------------------------------------------------ I4
    def test_invalid_project_names_are_rejected(self):
        for bad in ("../x", "", "a/b", "-lead", "x" * 65, "a b"):
            with self.assertRaises(ValueError, msg=bad):
                self.hub.create_job({"project": bad, "host": "h1",
                                     "cmd": ["true"]})
            with self.assertRaises(ValueError, msg=bad):
                self.hub.register_bundle(bad, self.root / "x.bundle", 0)
        with self.assertRaises(ValueError):
            self.hub.have_commits("../x", [self.head])

    # ------------------------------------------------------------ I9
    def test_have_commits_batch(self):
        self._bundle()
        missing = "0" * 40
        self.assertEqual(self.hub.have_commits("demo", [self.head, missing]),
                         [self.head])
        self.assertEqual(self.hub.have_commits("demo", []), [])

    # ------------------------------------------------------------ I1
    def test_claim_warns_and_refuses_when_slots_empty(self):
        config = make_config(self.root / "slots", hosts={"bare": {"slots": {}}})
        hub = Hub(config)
        try:
            hub.store.create_job(project="p", host="bare", slot="light",
                                 cmd=["true"], locks=[])
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertIsNone(hub.claim("bare", "w1"))
                self.assertIsNone(hub.claim("bare", "w1"))
            self.assertIn("WARNING", buffer.getvalue())
            self.assertEqual(buffer.getvalue().count("WARNING"), 1)  # 只警告一次
        finally:
            hub.close()
            hub.store.close()

    def test_heartbeat_from_unconfigured_host_warns(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.hub.heartbeat("ghost", "w1", [])
        self.assertIn("not configured", buffer.getvalue())

    # ------------------------------------------------------------ 主机组 / 按主机配置
    def test_group_job_records_targets_and_group(self):
        self.hub.config.raw["groups"] = {"linux": ["h1", "h2"]}
        self.hub.config.raw["hosts"]["h2"] = {"slots": {"heavy": 1, "light": 2}}
        job = self.hub.create_job({"project": "demo", "host": "linux",
                                   "cmd": ["true"]})
        self.assertEqual(job["group_name"], "linux")
        self.assertEqual(job["targets"], ["h1", "h2"])

    def test_unknown_group_or_host_is_rejected(self):
        with self.assertRaises(ValueError):
            self.hub.create_job({"project": "demo", "host": "nope",
                                 "cmd": ["true"]})

    def test_group_lock_display_expands_targets(self):
        self.hub.config.raw["groups"] = {"linux": ["h1", "h2"]}
        self.hub.config.raw["hosts"]["h2"] = {"slots": {"heavy": 1, "light": 1}}
        self.hub.store.seed_host("h2", {"heavy": 1, "light": 1}, [])
        job = self.hub.create_job({"project": "demo", "host": "linux",
                                   "cmd": ["true"],
                                   "client_version": __version__,
                                   "locks": ["fullgate@{host}"]})
        self.assertEqual(job["locks_display"], ["fullgate@{h1|h2}"])
        with self.hub.store.lock:
            self.hub.store.conn.execute(
                "UPDATE hosts SET last_seen=? WHERE name='h1'", (time.time() - 1000,))
        self.hub.store.heartbeat("h2", "w2", [])
        claimed = self.hub.store.claim("h2", "w2")
        self.assertEqual(claimed["locks_display"], ["fullgate@h2"])

    def test_group_uses_per_host_profile_override(self):
        self.hub.config.raw["groups"] = {"linux": ["h1", "h2"]}
        self.hub.config.raw["hosts"]["h2"] = {"slots": {"heavy": 1, "light": 2}}
        for host, env in (("h1", {"WHERE": "one"}), ("h2", {"WHERE": "two"})):
            (self.root / "profiles" / f"g@{host}.json").write_text(json.dumps({
                "name": f"g@{host}", "project": "g", "host": host,
                "slot": "light", "env": env, "artifacts": [],
                "locks": ["fullgate@{host}"]}),
                encoding="utf-8")
        self.hub.profiles = load_profiles(self.root / "profiles")

        job = self.hub.create_job({"project": "g", "host": "linux",
                                   "cmd": ["true"]})
        # h1 离线 → 由 h2 领取，profile 必须解析成 g@h2
        with self.hub.store.lock:
            self.hub.store.conn.execute(
                "UPDATE hosts SET last_seen=? WHERE name='h1'",
                (time.time() - 1000,))
        self.hub.store.seed_host("h2", {"heavy": 1, "light": 2}, [])
        self.hub.store.heartbeat("h2", "w2", [])
        claimed = self.hub.store.claim("h2", "w2")
        self.assertEqual(claimed["id"], job["id"])
        payload = self.hub._exec_payload(claimed)
        self.assertEqual(payload["profile_data"]["env"], {"WHERE": "two"})
        self.assertEqual(claimed["locks"], ["fullgate@h2"])
        self.assertEqual(payload["systemd"]["slice"], "calltable-light.slice")
        self.assertEqual(payload["systemd"]["memory_max"], "4G")
        self.assertEqual(payload["systemd"]["cpu_weight"], "200")

    def test_profile_can_override_resource_tier(self):
        (self.root / "profiles" / "demo@h1.json").write_text(json.dumps({
            "name": "demo@h1", "project": "demo", "host": "h1", "slot": "heavy",
            "memory_max": "30G", "cpu_weight": "600", "slice": "custom.slice",
            "artifacts": []}), encoding="utf-8")
        self.hub.profiles = load_profiles(self.root / "profiles")
        job = self.hub.create_job({"project": "demo", "host": "h1",
                                   "cmd": ["true"]})
        self.hub.store.heartbeat("h1", "w1", [])
        claimed = self.hub.store.claim("h1", "w1")
        payload = self.hub._exec_payload(claimed)
        self.assertEqual(payload["systemd"]["memory_max"], "30G")
        self.assertEqual(payload["systemd"]["cpu_weight"], "600")
        self.assertEqual(payload["systemd"]["slice"], "custom.slice")
        self.assertEqual(payload["cpu_rate_percent"], 70)

    # ------------------------------------------------------------ A/B 规则
    def _rule_profile(self, name: str, data: dict) -> None:
        (self.root / "profiles" / f"{name}@h1.json").write_text(
            json.dumps({"name": f"{name}@h1", "project": name, "host": "h1",
                        "artifacts": [], **data}), encoding="utf-8")
        self.hub.profiles = load_profiles(self.root / "profiles")

    def test_hub_applies_rules_and_explicit_values_win(self):
        self._rule_profile("gated", {
            "slot": "light", "locks": [], "timeout": "1h",
            "rules": [{"match": r"build/verify\.sh", "slot": "heavy",
                       "locks": ["gate@{host}"], "timeout": "3h",
                       "memory_max": "16G"}]})
        gate = self.hub.create_job({"project": "gated", "host": "h1",
                                    "cmd": ["bash", "build/verify.sh"]})
        self.assertEqual(gate["slot"], "heavy")
        self.assertEqual(gate["locks"], ["gate@{host}"])
        self.assertEqual(gate["timeout"], 3 * 3600)
        unit = self.hub.create_job({"project": "gated", "host": "h1",
                                    "cmd": ["dotnet", "test", "x.csproj"]})
        self.assertEqual(unit["slot"], "light")
        self.assertEqual(unit["locks"], [])
        self.assertEqual(unit["timeout"], 3600)
        override = self.hub.create_job({"project": "gated", "host": "h1",
                                        "cmd": ["bash", "build/verify.sh"],
                                        "client_version": __version__,
                                        "slot": "light", "timeout": 120,
                                        "no_lock": True})
        self.assertEqual(override["slot"], "light")
        self.assertEqual(override["locks"], [])
        self.assertEqual(override["timeout"], 120)

    def test_legacy_client_slot_locks_and_timeout_are_recomputed(self):
        self._rule_profile("gated", {
            "slot": "light", "locks": [], "timeout": "1h",
            "rules": [{"match": r"build/verify\.sh", "slot": "heavy",
                       "locks": ["gate@{host}"], "timeout": "4h",
                       "memory_max": "12G"}]})
        # 老客户端（0.1.0）把 profile 默认值当显式值发上来：slot=light、无锁、
        # timeout=3h。hub 必须忽略它们，按 rules 重算。
        legacy = self.hub.create_job({
            "project": "gated", "host": "h1",
            "cmd": ["bash", "build/verify.sh"],
            "slot": "light", "locks": [], "timeout": 10800})
        self.assertEqual(legacy["slot"], "heavy")
        self.assertEqual(legacy["locks"], ["gate@{host}"])
        self.assertEqual(legacy["timeout"], 4 * 3600)
        # 没带版本的请求同样按老客户端处理。
        unknown = self.hub.create_job({
            "project": "gated", "host": "h1",
            "cmd": ["bash", "build/verify.sh"], "slot": "light"})
        self.assertEqual(unknown["slot"], "heavy")
        # 新客户端显式值仍然优先。
        modern = self.hub.create_job({
            "project": "gated", "host": "h1",
            "cmd": ["bash", "build/verify.sh"],
            "client_version": __version__, "slot": "light"})
        self.assertEqual(modern["slot"], "light")

    def test_rule_memory_max_reaches_the_executor_payload(self):
        self._rule_profile("gated", {
            "slot": "light", "memory_max": "4G",
            "rules": [{"match": "gate", "slot": "heavy",
                       "locks": ["gate@{host}"], "memory_max": "16G"}]})
        gate = self.hub.create_job({"project": "gated", "host": "h1",
                                    "cmd": ["bash", "gate.sh"]})
        self.hub.store.heartbeat("h1", "w1", [])
        claimed = self.hub.store.claim("h1", "w1")
        payload = self.hub._exec_payload(claimed)
        self.assertEqual(claimed["id"], gate["id"])
        self.assertEqual(payload["systemd"]["memory_max"], "16G")
        self.assertEqual(payload["systemd"]["slice"], "calltable-heavy.slice")

    def test_profile_memory_is_only_used_for_its_own_slot(self):
        self._rule_profile("mixed", {
            "slot": "light", "memory_max": "6G",
            "rules": [{"match": "gate", "slot": "heavy", "locks": []}]})
        gate = self.hub.create_job({"project": "mixed", "host": "h1",
                                    "cmd": ["bash", "gate.sh"]})
        self.hub.store.heartbeat("h1", "w1", [])
        claimed = self.hub.store.claim("h1", "w1")
        payload = self.hub._exec_payload(claimed)
        self.assertEqual(claimed["id"], gate["id"])
        # heavy 走 resource_defaults，不继承 light 的 6G
        self.assertEqual(payload["systemd"]["memory_max"], "20G")

    def test_heartbeat_stores_system_metrics_for_hosts_view(self):
        self.hub.heartbeat("h1", "w1", [], system={
            "disk_free_gb": 120.5, "disk_runs_gb": 33.0, "disk_low": True,
            "disk_reason": "free low", "mem_free_gb": 12.0,
            "mem_total_gb": 31.0, "load1": 3.5})
        host = {h["name"]: h for h in self.hub.store.hosts()}["h1"]
        self.assertTrue(host["disk_low"])
        self.assertEqual(host["system"]["disk_free_gb"], 120.5)
        self.assertEqual(host["system"]["load1"], 3.5)

    def test_queue_info_is_exposed_on_job_endpoints(self):
        jobs = []
        for _ in range(3):
            jobs.append(self.hub.create_job({
                "project": "demo", "host": "h1", "slot": "heavy",
                "cmd": ["true"]}))
        info = self.hub.with_queue_info(self.hub.store.jobs(status="queued"))
        by_id = {job["id"]: job for job in info}
        self.assertEqual(by_id[jobs[0]["id"]]["ahead"], 0)
        self.assertEqual(by_id[jobs[1]["id"]]["ahead"], 1)
        self.assertEqual(by_id[jobs[2]["id"]]["ahead"], 2)
        self.assertIn("queued_seconds", by_id[jobs[0]["id"]])

    def test_project_names_are_used_verbatim(self):
        (self.root / "profiles" / "demo@h1.json").write_text(json.dumps({
            "name": "demo@h1", "project": "demo", "host": "h1",
            "slot": "light", "artifacts": []}), encoding="utf-8")
        self.hub.profiles = load_profiles(self.root / "profiles")
        job = self.hub.create_job({"project": "demo", "host": "h1",
                                   "cmd": ["true"]})
        self.assertEqual(job["project"], "demo")
        self.assertNotIn("notice", job)
        self.assertEqual(job["profile"], "demo@h1")

    def test_mirror_paths_follow_the_project_name_and_reject_escapes(self):
        self.assertEqual(self.hub.mirror_for("demo").name, "demo.git")
        with self.assertRaises(ValueError):
            self.hub.mirror_for("../evil")

    # ------------------------------------------------------------ I6
    def test_janitor_purges_expired_job_data_and_files(self):
        bundle_id, bundle_path, _ = self._bundle()
        job = self.hub.create_job({
            "project": "demo", "host": "h1", "cmd": ["true"],
            "sha": self.head, "bundle_id": bundle_id})
        self.hub.store.finish(job["id"], status="succeeded", exit_code=0,
                              cleanup_at=time.time() - 5)
        self.hub.store.append_log(job["id"], b"hello")
        artifact = self.hub.artifact_dir / job["id"] / "a.bin"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"data")
        self.hub.store.add_artifact(job["id"], "a.bin", 4, "sha", str(artifact))

        self.hub.janitor_once()

        self.assertEqual(self.hub.store.read_logs(job["id"], 0)[0], b"")
        self.assertEqual(self.hub.store.artifacts(job["id"]), [])
        self.assertFalse(artifact.exists())
        self.assertFalse(bundle_path.exists())
        self.assertIsNone(self.hub.store.job_bundle(job["id"]))

    def test_sample_computes_total_and_peak_cores(self):
        job = self.hub.store.create_job(project="demo", host="h1", slot="light",
                                        cmd=["true"], locks=[])
        self.hub.store.add_sample(job["id"], 1_000_000, 10, 10)
        time.sleep(0.25)
        self.hub.store.add_sample(job["id"], 3_000_000, 20, 20)
        row = self.hub.store.job(job["id"])
        self.assertAlmostEqual(row["cpu_total_sec"], 3.0, places=3)
        self.assertGreater(row["cpu_peak_cores"], 0.0)

    def test_thick_bundle_is_atomic_and_cached(self):
        self._bundle()
        import threading
        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(self.hub.thick_bundle("demo", self.head))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({str(path) for path in results}), 1)
        bundle = results[0]
        self.assertTrue(bundle.exists())
        # 打的必须是能 fetch 的合法 bundle
        self.assertEqual(gitops.bundle_prerequisites(bundle), [])
        self.assertEqual(gitops.git(["bundle", "list-heads", str(bundle)]).returncode, 0)

    def test_start_janitor_returns_without_blocking(self):
        config = make_config(self.root / "janitor", hosts={
            "h1": {"slots": {"light": 1}}})
        hub = Hub(config)
        started = time.time()
        hub.start_janitor()
        # 只要求“不在调用线程里同步跑 janitor”；放宽阈值避免 CI/本机负载抖动。
        self.assertLess(time.time() - started, 5.0)
        hub.close()
        hub.store.close()

    def test_upstream_once_fetches_main(self):
        sha = self.hub.upstream_once("demo", str(self.repo))
        self.assertEqual(sha, self.head)
        mirror = self.hub.mirror_dir / "demo.git"
        self.assertEqual(git(mirror, "rev-parse", "refs/calltable/upstream/main"),
                         self.head)

    def test_upstream_failure_is_reported(self):
        with self.assertRaises(gitops.GitError):
            self.hub.upstream_once("demo", str(self.root / "missing.git"))

    def test_upstream_config_and_interval(self):
        config = make_config(self.root / "upstream-config",
                             upstreams={"demo": "git@example/demo.git"},
                             upstream_interval=90)
        self.assertEqual(config.upstreams, {"demo": "git@example/demo.git"})
        self.assertEqual(config.upstream_interval, 90)
        config2 = make_config(self.root / "upstream-config2",
                              upstream_interval=1)
        self.assertEqual(config2.upstream_interval, 30)

    def test_janitor_prunes_old_keep_refs(self):
        self._bundle()
        mirror = self.hub.mirror_dir / "demo.git"
        self.assertTrue(gitops.keep_refs(mirror))
        self.assertGreater(gitops.prune_keep_refs(mirror, time.time() + 1), 0)
        self.assertEqual(gitops.keep_refs(mirror), [])


if __name__ == "__main__":
    unittest.main()
