"""profile rules: slot / locks / timeout / memory_max defaults by command shape."""

from __future__ import annotations

import unittest
from pathlib import Path

from calltable.profiles import Rule, load_profiles, match_rule, resolve_job, resolve_host

REPO = Path(__file__).resolve().parents[1]
PROFILES = load_profiles(REPO / "profiles" / "examples")

GATE = ["bash", "build/verify.sh"]
SMOKE = ["bash", "build/smoke.sh"]
INTEGRATION = ["dotnet", "test", "tests/Example.IntegrationTests/"
               "Example.IntegrationTests.csproj"]
UNIT = ["dotnet", "test", "tests/Example.Core.Tests/"
        "Example.Core.Tests.csproj", "--filter", "FullyQualifiedName~Foo"]
BUILD = ["dotnet", "build", "Example.slnx"]

PNPM_FULL = ["pnpm", "test"]
PNPM_E2E = ["pnpm", "run", "test:e2e"]
PNPM_UNIT = ["pnpm", "test:unit"]
PNPM_INSTALL = ["pnpm", "install", "--frozen-lockfile"]

PYTEST_INT = ["pytest", "tests/integration", "-q"]
PYTEST_SLOW = ["pytest", "-m", "slow"]
PYTEST_UNIT = ["pytest", "-q"]


class DotnetRuleTests(unittest.TestCase):
    def setUp(self):
        self.profile = PROFILES["dotnet@runner-a"]

    def _resolve(self, cmd, **kw):
        return resolve_job(self.profile, cmd, **kw)

    def test_gate_script_is_heavy_with_a_per_host_lock(self):
        for cmd in (GATE, SMOKE):
            resolved = self._resolve(cmd)
            self.assertEqual(resolved.slot, "heavy")
            self.assertEqual(resolved.locks, ["dotnet-fullgate@{host}"])
            self.assertEqual(resolved.timeout, 2 * 3600)
            self.assertEqual(resolved.memory_max, "12G")
            self.assertIsNotNone(resolved.rule)

    def test_unit_test_is_light_and_unlocked(self):
        for cmd in (UNIT, BUILD):
            resolved = self._resolve(cmd)
            self.assertEqual(resolved.slot, "light", cmd)
            self.assertEqual(resolved.locks, [], cmd)
            self.assertEqual(resolved.timeout, 3600, cmd)
            self.assertIsNone(resolved.rule)
            self.assertIsNone(resolved.memory_max)

    def test_integration_tests_are_heavy_even_without_a_gate_script(self):
        resolved = self._resolve(INTEGRATION)
        self.assertEqual(resolved.slot, "heavy")
        self.assertEqual(resolved.locks, [])
        self.assertEqual(resolved.timeout, 2 * 3600)

    def test_explicit_values_win(self):
        resolved = self._resolve(GATE, slot="light", locks=["extra"],
                                 timeout=120)
        self.assertEqual(resolved.slot, "light")
        self.assertEqual(resolved.locks, ["dotnet-fullgate@{host}", "extra"])
        self.assertEqual(resolved.timeout, 120)

    def test_no_lock_keeps_explicit_ones(self):
        resolved = self._resolve(GATE, no_lock=True)
        self.assertEqual(resolved.locks, [])
        self.assertEqual(resolved.slot, "heavy")
        resolved = resolve_job(self.profile, GATE, no_lock=True, locks=["extra"])
        self.assertEqual(resolved.locks, ["extra"])
        self.assertEqual(resolved.slot, "heavy")

    def test_rule_locks_are_added_to_profile_locks(self):
        # A rule that omits `locks` inherits the profile locks; explicit --lock
        # values are appended on top.
        import copy

        profile = copy.copy(PROFILES["dotnet@runner-a"])
        profile.locks = ["global-lock"]
        profile.rules = [Rule(match="dotnet", locks=None)]
        resolved = resolve_job(profile, UNIT)
        self.assertEqual(resolved.locks, ["global-lock"])
        resolved = resolve_job(profile, UNIT, locks=["extra"])
        self.assertEqual(resolved.locks, ["global-lock", "extra"])


class PnpmAndPytestRuleTests(unittest.TestCase):
    def setUp(self):
        self.pnpm = PROFILES["pnpm@runner-a"]
        self.pytest = PROFILES["pytest@runner-a"]

    def test_pnpm_full_and_e2e_runs_are_heavy(self):
        for cmd in (PNPM_FULL, PNPM_E2E):
            resolved = resolve_job(self.pnpm, cmd)
            self.assertEqual(resolved.slot, "heavy", cmd)
            self.assertEqual(resolved.locks, ["pnpm-fullgate@{host}"], cmd)
            self.assertEqual(resolved.timeout, 3600, cmd)

    def test_pnpm_unit_and_install_are_light(self):
        for cmd in (PNPM_UNIT, PNPM_INSTALL):
            resolved = resolve_job(self.pnpm, cmd)
            self.assertEqual(resolved.slot, "light", cmd)
            self.assertEqual(resolved.locks, [], cmd)
            self.assertEqual(resolved.timeout, 1800, cmd)

    def test_pytest_integration_and_slow_are_heavy(self):
        for cmd in (PYTEST_INT, PYTEST_SLOW):
            resolved = resolve_job(self.pytest, cmd)
            self.assertEqual(resolved.slot, "heavy", cmd)
            self.assertEqual(resolved.locks, [], cmd)
            self.assertEqual(resolved.timeout, 45 * 60, cmd)

    def test_pytest_unit_is_light(self):
        resolved = resolve_job(self.pytest, PYTEST_UNIT)
        self.assertEqual(resolved.slot, "light")
        self.assertEqual(resolved.timeout, 20 * 60)

    def test_example_profiles_are_documented(self):
        # Keep the README profile example and the shipped examples in sync.
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("profiles/examples/", readme)
        for name in ("dotnet@runner-a", "pnpm@runner-a", "pytest@runner-a"):
            self.assertIn(name, PROFILES)
        self.assertEqual(sorted(p.stem for p in
                                (REPO / "profiles" / "examples").glob("*.json")),
                         ["dotnet@runner-a", "pnpm@runner-a", "pytest@runner-a"])


class RuleParsingTests(unittest.TestCase):
    def test_locks_null_inherits_profile_and_empty_clears(self):
        profile = PROFILES["dotnet@runner-a"]
        inherit = Rule.from_dict({"match": "x"})
        clear = Rule.from_dict({"match": "x", "locks": []})
        self.assertIsNone(inherit.locks)
        self.assertEqual(clear.locks, [])
        self.assertEqual(len(profile.rules), 2)

    def test_invalid_regex_is_skipped(self):
        profile = PROFILES["dotnet@runner-a"]
        profile = type(profile)(**{**profile.__dict__,
                                   "rules": [Rule(match="("),
                                             Rule(match="verify")]})
        rule, command = match_rule(profile, ["bash", "build/verify.sh"])
        self.assertIsNotNone(rule)
        self.assertEqual(rule.match, "verify")
        self.assertIn("verify.sh", command)

    def test_no_profile_falls_back_to_defaults(self):
        resolved = resolve_job(None, ["true"], default_slot="light")
        self.assertEqual(resolved.slot, "light")
        self.assertEqual(resolved.locks, [])
        self.assertEqual(resolved.timeout, 3 * 3600)

    def test_example_projects_infer_their_host(self):
        self.assertEqual(resolve_host("dotnet", profiles=PROFILES), "runner-a")
        self.assertEqual(resolve_host("pnpm", profiles=PROFILES), "runner-a")
        self.assertEqual(resolve_host("pytest", profiles=PROFILES), "runner-a")
        with self.assertRaises(ValueError):
            resolve_host("unknown-project", profiles=PROFILES)


if __name__ == "__main__":
    unittest.main()
