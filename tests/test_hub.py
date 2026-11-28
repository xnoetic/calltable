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
from unittest import mock

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

    def _overflow_group(self):
        """h1 + 排在最后的 provider 主机 cr（溢出组）。"""
        self.hub.config.raw["hosts"]["cr"] = {
            "slots": {"light": 2}, "provider": "cloudrun",
            "cloudrun": {"project": "demo-cloud"}}
        self.hub.config.raw["groups"] = {"pool": ["h1", "cr"],
                                         "cloud-only": ["cr"]}

    def _write_profile(self, name: str, **data):
        # Windows 文件名不能含 *：通配 profile 靠 JSON 里的 name 生效，文件名换个写法。
        stem = name.replace("*", "_any_")
        (self.root / "profiles" / f"{stem}.json").write_text(
            json.dumps({"name": name, "slot": "light", **data}), encoding="utf-8")
        self.hub.profiles = load_profiles(self.root / "profiles")

    def _submit(self, host: str, project: str = "demo") -> dict:
        return self.hub.create_job({"project": project, "host": host,
                                    "cmd": ["true"],
                                    "client_version": __version__})

    def test_provider_host_joins_a_group_only_with_its_own_profile(self):
        self._overflow_group()
        self.assertEqual(self._submit("pool")["targets"], ["h1"])
        # 通配 profile 不算数。
        self._write_profile("demo@*")
        self._write_profile("*@cr")
        self.assertEqual(self._submit("pool")["targets"], ["h1"])
        self._write_profile("demo@cr", unsupported=True, note="no image")
        self.assertEqual(self._submit("pool")["targets"], ["h1"])
        self._write_profile("demo@cr")
        job = self._submit("pool")
        self.assertEqual(job["targets"], ["h1", "cr"])
        self.assertEqual(job["group_name"], "pool")
        # 别的项目照样只发给 h1。
        self.assertEqual(self._submit("pool", project="other")["targets"], ["h1"])

    def test_profile_file_named_for_another_project_does_not_count(self):
        self._overflow_group()
        self._write_profile("demo@cr", project="other", host="cr")
        self.assertEqual(self._submit("pool")["targets"], ["h1"])

    def test_group_job_keeps_only_the_first_eligible_provider_host(self):
        self._overflow_group()
        self.hub.config.raw["hosts"]["cr2"] = {
            "slots": {"light": 2}, "provider": "cloudrun",
            "cloudrun": {"project": "demo-cloud"}}
        self.hub.config.raw["groups"]["pool"] = ["h1", "cr", "cr2"]
        self._write_profile("demo@cr2")
        # cr 没有 profile：第一台符合条件的是 cr2。
        self.assertEqual(self._submit("pool")["targets"], ["h1", "cr2"])
        self._write_profile("demo@cr")
        self.assertEqual(self._submit("pool")["targets"], ["h1", "cr"])

    def test_group_with_several_provider_hosts_warns_at_start(self):
        self._overflow_group()
        self.hub.config.raw["hosts"]["cr2"] = {
            "slots": {"light": 2}, "provider": "cloudrun",
            "cloudrun": {"project": "demo-cloud"}}
        self.hub.config.raw["groups"]["pool"] = ["h1", "cr", "cr2"]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            hub = Hub(self.hub.config, cloudrun_api=mock.Mock())
        try:
            self.assertIn("group pool lists several cloudrun hosts (cr, cr2)",
                          buffer.getvalue())
            self.assertNotIn("group cloud-only lists several", buffer.getvalue())
        finally:
            hub.close()
            hub.store.close()

    def test_group_without_an_eligible_host_is_rejected(self):
        self._overflow_group()
        with self.assertRaises(ValueError) as ctx:
            self._submit("cloud-only")
        self.assertIn("demo@<host> profile", str(ctx.exception))

    def test_direct_submission_to_a_provider_host_needs_no_profile(self):
        self._overflow_group()
        job = self._submit("cr")
        self.assertEqual(job["targets"], ["cr"])
        self.assertEqual(job["group_name"], "")

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

    # ------------------------------------------------------------ 通配 profile（H11）
    def _wildcard_profile(self, filename: str, data: dict) -> None:
        # Windows 文件名不能含 `*`：通配写在 JSON 的 name 字段里，文件名随意。
        (self.root / "profiles" / filename).write_text(
            json.dumps({"artifacts": [], **data}), encoding="utf-8")
        self.hub.profiles = load_profiles(self.root / "profiles")

    def test_host_wildcard_profile_applies_to_projects_without_their_own(self):
        self._wildcard_profile("any@h1.json", {
            "name": "*@h1", "slot": "heavy", "locks": ["box@{host}"],
            "timeout": "5m", "env": {"WHERE": "h1-any"}})
        job = self.hub.create_job({"project": "wild", "host": "h1",
                                   "cmd": ["true"]})
        self.assertEqual(job["profile"], "*@h1")
        self.assertEqual(job["slot"], "heavy")
        self.assertEqual(job["locks"], ["box@{host}"])
        self.assertEqual(job["timeout"], 300)
        # 有专属 profile 的项目仍然用自己的（project@host 优先于 *@host）。
        own = self.hub.create_job({"project": "demo", "host": "h1",
                                   "cmd": ["true"]})
        self.assertEqual(own["profile"], "demo@h1")
        self.assertEqual(own["slot"], "light")

    def test_global_wildcard_profile_applies_to_any_project(self):
        self._wildcard_profile("any@any.json", {
            "name": "*@*", "slot": "heavy", "timeout": "7m"})
        job = self.hub.create_job({"project": "wild", "host": "h1",
                                   "cmd": ["true"]})
        self.assertEqual(job["profile"], "*@*")
        self.assertEqual(job["slot"], "heavy")
        self.assertEqual(job["timeout"], 420)

    def test_project_wildcard_is_used_only_where_no_host_profile_exists(self):
        self.hub.config.raw["hosts"]["h2"] = {"slots": {"heavy": 1, "light": 2}}
        self._wildcard_profile("p@h1.json", {
            "name": "p@h1", "slot": "light", "timeout": "1m"})
        self._wildcard_profile("p@any.json", {
            "name": "p@*", "slot": "heavy", "timeout": "2m"})
        self._wildcard_profile("any@h2.json", {
            "name": "*@h2", "slot": "light", "timeout": "3m"})
        self._wildcard_profile("any@any.json", {
            "name": "*@*", "slot": "light", "timeout": "4m"})
        on_h1 = self.hub.create_job({"project": "p", "host": "h1",
                                     "cmd": ["true"]})
        on_h2 = self.hub.create_job({"project": "p", "host": "h2",
                                     "cmd": ["true"]})
        other_h1 = self.hub.create_job({"project": "other", "host": "h1",
                                        "cmd": ["true"]})
        other_h2 = self.hub.create_job({"project": "other", "host": "h2",
                                        "cmd": ["true"]})
        # 优先级：project@host > project@* > *@host > *@*
        self.assertEqual((on_h1["profile"], on_h1["slot"], on_h1["timeout"]),
                         ("p@h1", "light", 60))
        self.assertEqual((on_h2["profile"], on_h2["slot"], on_h2["timeout"]),
                         ("p@*", "heavy", 120))
        self.assertEqual((other_h2["profile"], other_h2["timeout"]),
                         ("*@h2", 180))
        self.assertEqual((other_h1["profile"], other_h1["timeout"]),
                         ("*@*", 240))

    def _claim_on_h2(self, job: dict) -> dict:
        """h1 离线 → 组任务由 h2 领取。"""
        with self.hub.store.lock:
            self.hub.store.conn.execute(
                "UPDATE hosts SET last_seen=? WHERE name='h1'",
                (time.time() - 1000,))
        self.hub.store.seed_host("h2", {"heavy": 1, "light": 2}, [])
        self.hub.store.heartbeat("h2", "w2", [])
        claimed = self.hub.store.claim("h2", "w2")
        self.assertEqual(claimed["id"], job["id"])
        return claimed

    def test_wildcard_profile_is_re_resolved_for_the_executing_host(self):
        # 执行器只用 hub 下发的 profile_data；hub 在领取时按实际主机重新解析
        # env/env_files/setup/artifacts，通配 profile 在这一步也必须生效。
        # slot/locks/timeout 在提交时按组内首成员定死，这里不断言它们。
        self.hub.config.raw["groups"] = {"linux": ["h1", "h2"]}
        self.hub.config.raw["hosts"]["h2"] = {"slots": {"heavy": 1, "light": 2}}
        for host, lock in (("h1", "box@{host}"), ("h2", "gpu@{host}")):
            self._wildcard_profile(f"any@{host}.json", {
                "name": f"*@{host}", "slot": "light", "locks": [lock],
                "env": {"WHERE": host}, "env_files": [f"/etc/{host}.sh"],
                "setup": [f"echo {host}"]})
        job = self.hub.create_job({"project": "wild", "host": "linux",
                                   "cmd": ["true"]})
        self.assertEqual(job["profile"], "*@h1")
        payload = self.hub._exec_payload(self._claim_on_h2(job))
        data = payload["profile_data"]
        self.assertEqual(data["name"], "*@h2")
        self.assertEqual(data["env"], {"WHERE": "h2"})
        self.assertEqual(data["env_files"], ["/etc/h2.sh"])
        self.assertEqual(data["setup"], ["echo h2"])

    def test_host_wildcard_is_not_carried_over_to_another_host(self):
        # *@h1 只管 h1：组任务落到没有任何 profile 的 h2 时，不能把 h1 的
        # env_files（只在 h1 上存在）套过去，否则任务会以 97 失败。
        self.hub.config.raw["groups"] = {"linux": ["h1", "h2"]}
        self.hub.config.raw["hosts"]["h2"] = {"slots": {"heavy": 1, "light": 2}}
        self._wildcard_profile("any@h1.json", {
            "name": "*@h1", "slot": "light",
            "env": {"WHERE": "h1"}, "env_files": ["/etc/h1.sh"]})
        job = self.hub.create_job({"project": "wild", "host": "linux",
                                   "cmd": ["true"]})
        self.assertEqual(job["profile"], "*@h1")
        payload = self.hub._exec_payload(self._claim_on_h2(job))
        self.assertEqual(payload["profile_data"], {})

    def test_profile_named_for_another_project_is_still_rejected(self):
        # 归属校验只放行通配：name 指向本项目、project 却是别的项目仍然 400。
        self._wildcard_profile("mixup@h1.json", {
            "name": "mixup@h1", "project": "someone-else", "host": "h1"})
        with self.assertRaises(ValueError) as ctx:
            self.hub.create_job({"project": "mixup", "host": "h1",
                                 "cmd": ["true"]})
        self.assertIn("does not belong", str(ctx.exception))

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

    # ------------------------------------------------------------ 主机级资源档位
    def _host_tiers(self):
        """h1 只覆盖 light.memory_max；h2 覆盖 heavy 的三个键。"""
        hosts = self.hub.config.raw["hosts"]
        hosts["h1"]["resource_defaults"] = {"light": {"memory_max": "14G"}}
        hosts["h2"] = {"slots": {"heavy": 1, "light": 2},
                       "resource_defaults": {"heavy": {
                           "memory_max": "28G", "cpu_weight": "800",
                           "slice": "big.slice"}}}
        self.hub.store.seed_host("h2", {"heavy": 1, "light": 2}, [])

    def _payload_on(self, host: str, cmd=("true",), project="demo",
                    slot=None) -> dict:
        body = {"project": project, "host": host, "cmd": list(cmd),
                "client_version": __version__}
        if slot:
            body["slot"] = slot
        job = self.hub.create_job(body)
        claimer = host if host in self.hub.config.hosts else "h2"
        self.hub.store.heartbeat(claimer, "w", [])
        claimed = self.hub.store.claim(claimer, "w")
        self.assertEqual(claimed["id"], job["id"])
        payload = self.hub._exec_payload(claimed)["systemd"]
        self.hub.store.finish(job["id"], status="succeeded", exit_code=0)
        return payload

    def test_host_tier_overrides_the_hub_tier_for_that_host_only(self):
        self._host_tiers()
        self.assertEqual(self._payload_on("h2", slot="heavy"),
                         {"memory_max": "28G", "cpu_weight": "800",
                          "slice": "big.slice"})
        # h1 的 heavy 没覆盖：仍是 hub 级档位。
        self.assertEqual(self._payload_on("h1", slot="heavy"),
                         {"memory_max": "20G", "cpu_weight": "400",
                          "slice": "calltable-heavy.slice"})

    def test_partial_host_tier_merges_per_key(self):
        self._host_tiers()
        self.assertEqual(self._payload_on("h1", slot="light"),
                         {"memory_max": "14G", "cpu_weight": "200",
                          "slice": "calltable-light.slice"})

    def test_rule_and_profile_still_win_over_the_host_tier(self):
        self._host_tiers()
        self._rule_profile("tiered", {
            "slot": "light", "memory_max": "6G", "cpu_weight": "50",
            "rules": [{"match": "gate", "slot": "heavy", "locks": [],
                       "memory_max": "16G"}]})
        hosts = self.hub.config.raw["hosts"]
        hosts["h1"]["resource_defaults"]["heavy"] = {"memory_max": "30G"}
        light = self._payload_on("h1", project="tiered")
        self.assertEqual(light["memory_max"], "6G")   # profile 自己的 slot
        self.assertEqual(light["cpu_weight"], "50")
        heavy = self._payload_on("h1", cmd=("bash", "gate.sh"), project="tiered")
        self.assertEqual(heavy["memory_max"], "16G")  # 规则
        plain = self._payload_on("h1", cmd=("bash", "other.sh"), project="tiered",
                                 slot="heavy")
        self.assertEqual(plain["memory_max"], "30G")  # 不是 profile 的 slot → 主机档位

    def test_group_job_gets_the_tier_of_the_host_that_claimed_it(self):
        self._host_tiers()
        self.hub.config.raw["groups"] = {"linux": ["h1", "h2"]}
        with self.hub.store.lock:
            self.hub.store.conn.execute(
                "UPDATE hosts SET last_seen=? WHERE name='h1'", (time.time() - 1000,))
        tier = self._payload_on("linux", slot="heavy")
        self.assertEqual(tier["memory_max"], "28G")

    def test_bad_host_tiers_are_rejected(self):
        from calltable.config import ConfigError, load_config
        for bad in ("8G", ["light"], {"light": "8G"}):
            raw = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
            raw["hosts"]["h1"]["resource_defaults"] = bad
            path = self.root / "bad.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(str(path))
            self.assertIn("host h1", str(ctx.exception))
            self.hub.config.raw["hosts"]["h1"]["resource_defaults"] = bad
            with self.assertRaises(ConfigError):
                Hub(self.hub.config)
        # 与 hub 级一致：未知 slot / 键照收，值转成字符串，null 表示不覆盖。
        self.hub.config.raw["hosts"]["h1"]["resource_defaults"] = {
            "gpu": {"memory_max": 64, "extra": "x"}, "light": None}
        self.assertEqual(self.hub.config.hosts["h1"].resource_defaults,
                         {"gpu": {"memory_max": "64", "extra": "x"}})

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


class HubServerTests(unittest.TestCase):
    """hub 监听时不能做反向 DNS。

    ``HTTPServer.server_bind`` 会调 ``socket.getfqdn()``；macOS 上这句走
    mDNSResponder，解析服务不可用时会卡几十秒，期间 hub 还没 listen。
    """

    def test_bind_does_not_look_up_reverse_dns(self):
        from unittest import mock

        from calltable.hub import Handler, HubServer

        def explode(*args, **kwargs):
            raise AssertionError("hub must not resolve reverse DNS at bind time")

        with mock.patch("socket.getfqdn", side_effect=explode):
            server = HubServer(("127.0.0.1", 0), Handler)
            try:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)
            finally:
                server.server_close()

    def test_extra_binds_drop_blanks_and_duplicates(self):
        from calltable.config import Config

        def make(raw):
            return Config(path=Path("c.json"), raw=raw, token="t",
                          data_dir=Path("data"))

        config = make({"hub_bind": "192.0.2.1",
                       "hub_extra_binds": ["100.64.0.10", "", "192.0.2.1",
                                           "100.64.0.10"]})
        self.assertEqual(config.hub_extra_binds, ["100.64.0.10"])
        self.assertEqual(make({"hub_extra_binds": "10.0.0.5"}).hub_extra_binds,
                         ["10.0.0.5"])
        self.assertEqual(make({}).hub_extra_binds, [])


if __name__ == "__main__":
    unittest.main()
