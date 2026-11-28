"""客户端可移植性：配置查找、doctor、setup、提交人格式、主机自动选择（追加需求 1-7）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from calltable import cli, doctor, setup as setup_module
from calltable.config import (ConfigError, DEFAULT_HUB_URL, DEFAULT_OWNER_TAILNET,
                             find_config, load_config)
from calltable.profiles import find_profile, load_profiles, resolve_host
from tests.helpers import make_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


class ConfigSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.user_cfg = self.home / ".config" / "calltable" / "config.json"
        self.user_cfg.parent.mkdir(parents=True)
        self.user_cfg.write_text(json.dumps({"token": "user-token"}), encoding="utf-8")
        self.explicit = self.root / "explicit.json"
        self.explicit.write_text(json.dumps({"token": "explicit-token"}),
                                 encoding="utf-8")
        self.env_cfg = self.root / "env.json"
        self.env_cfg.write_text(json.dumps({"token": "env-token"}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_explicit_env_user(self):
        with mock.patch("pathlib.Path.home", return_value=self.home):
            self.assertEqual(find_config(str(self.explicit)), self.explicit)
            with mock.patch.dict(os.environ, {"CALLTABLE_CONFIG": str(self.env_cfg)}):
                self.assertEqual(find_config(None), self.env_cfg)
                self.assertEqual(find_config(str(self.explicit)), self.explicit)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CALLTABLE_CONFIG", None)
                self.assertEqual(find_config(None), self.user_cfg)

    def test_missing_everything_is_an_actionable_error(self):
        # 测试环境总是设置 CALLTABLE_CONFIG（指向一个不存在的临时文件），
        # 这个用例要模拟「什么都没有」，所以先清掉它。
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLTABLE_CONFIG", None)
            with mock.patch("pathlib.Path.home",
                            return_value=self.root / "nohome"):
                with self.assertRaises(ConfigError) as ctx:
                    find_config(None)
        self.assertIn("ct setup", str(ctx.exception))

    def test_profiles_dir_falls_back_to_the_install(self):
        from calltable.config import Config
        cfg = Config(path=self.root / "cfg" / "config.json",
                     raw={"token": "t"}, token="t", data_dir=self.root / "data")
        self.assertEqual(cfg.profiles_dir.name, "profiles")
        self.assertTrue((cfg.profiles_dir / "examples" / "pytest@runner-a.json").exists())
        custom = self.root / "custom-profiles"
        custom.mkdir()
        with mock.patch.dict(os.environ, {"CALLTABLE_PROFILES_DIR": str(custom)}):
            self.assertEqual(cfg.profiles_dir, custom)

    def test_explicit_relative_paths_follow_config_not_cwd(self):
        directory = self.root / "service"
        profiles = directory / "custom-profiles"
        profiles.mkdir(parents=True)
        path = directory / "config.json"
        path.write_text(json.dumps({"token": "t", "runs_root": "runs",
                                    "mirror_root": "mirrors",
                                    "profiles_dir": "custom-profiles"}), encoding="utf-8")
        config = load_config(str(path))
        self.assertEqual(config.runs_root, directory.resolve() / "runs")
        self.assertEqual(config.mirror_root, directory.resolve() / "mirrors")
        self.assertEqual(config.profiles_dir, profiles.resolve())

    def test_default_hub_and_owner_tailnet(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLTABLE_CONFIG", None)
            with mock.patch("pathlib.Path.home", return_value=self.home):
                config = load_config(None)
        self.assertEqual(config.hub_url, DEFAULT_HUB_URL)
        self.assertTrue(DEFAULT_HUB_URL.startswith("http://127.0.0.1"))
        self.assertEqual(config.owner_tailnet, DEFAULT_OWNER_TAILNET)
        self.assertEqual(config.owner_tailnet, "")

    def test_doctor_can_load_config_without_token(self):
        path = self.root / "notoken.json"
        path.write_text(json.dumps({"hub_url": "http://h:1"}), encoding="utf-8")
        config = load_config(str(path), require_token=False)
        self.assertEqual(config.token, "")
        with self.assertRaises(ConfigError):
            load_config(str(path))


class SubmitterTests(unittest.TestCase):
    def test_default_submitter_includes_host_user_repo_branch(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Path(tmp) / "my-worktree"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "feature/x")
            git(repo, "config", "user.email", "t@e")
            git(repo, "config", "user.name", "t")
            (repo / "a").write_text("a", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "init")
            with mock.patch("calltable.cli.socket.gethostname", return_value="host1"), \
                    mock.patch("calltable.cli.getpass.getuser", return_value="agent-user"):
                submitter = cli.make_submitter(repo, "claude")
        self.assertEqual(submitter, "host1/agent-user/my-worktree@feature/x")

    def test_detached_head_uses_short_sha(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.email", "t@e")
            git(repo, "config", "user.name", "t")
            (repo / "a").write_text("a", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "init")
            sha = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-q", "--detach", sha)
            with mock.patch("calltable.cli.socket.gethostname", return_value="h"), \
                    mock.patch("calltable.cli.getpass.getuser", return_value="u"):
                submitter = cli.make_submitter(repo)
        self.assertTrue(submitter.endswith("@" + sha[:8]), submitter)


class ProfileFileTests(unittest.TestCase):
    def test_example_profiles_are_generic_and_loadable(self):
        profiles = load_profiles(Path(__file__).resolve().parents[1] / "profiles" / "examples")
        for key in ("dotnet@runner-a", "pnpm@runner-a", "pytest@runner-a"):
            self.assertIn(key, profiles)
            profile = profiles[key]
            self.assertEqual(profile.host, "runner-a")
            self.assertTrue(profile.rules)
            self.assertTrue(all("/home/" not in path for path in profile.env_files))
        self.assertEqual(profiles["dotnet@runner-a"].locks, [])
        self.assertEqual(profiles["dotnet@runner-a"].rules[1].locks,
                         ["dotnet-fullgate@{host}"])
        self.assertIn("**/TestResults/*.trx",
                      profiles["dotnet@runner-a"].artifacts)
        self.assertEqual(profiles["pnpm@runner-a"].rules[0].slot, "heavy")
        self.assertEqual(profiles["pytest@runner-a"].rules[0].slot, "heavy")
        # Examples without a matching project are not invented by the loader.
        self.assertNotIn("otherproject@runner-a", profiles)

    def test_examples_subdirectory_is_not_loaded(self):
        root = Path(__file__).resolve().parents[1] / "profiles"
        self.assertTrue((root / "examples").is_dir())
        self.assertEqual(list(root.glob("*.json")), [])
        self.assertEqual(load_profiles(root), {})
        for path in (root / "examples").glob("*.json"):
            self.assertNotIn(path.stem, load_profiles(root))


class DoctorVersionTests(unittest.TestCase):
    def _evaluate(self, client, hub):
        return doctor.evaluate(
            owner_tailnet="example.ts.net", tailscale_installed=True,
            tailnet="example.ts.net", online=True, python_ok=True,
            git_ok=True, git_version_text="2.40", hub_ok=True, auth_ok=True,
            hub_url="http://hub:1", client_version=client, hub_version=hub)

    def test_older_client_is_not_ready(self):
        code, line = self._evaluate("0.1.0", "0.2.0")
        self.assertEqual(code, 3)
        self.assertIn("client 0.1.0 older than hub 0.2.0", line)
        self.assertIn("ct self-update", line)

    def test_same_or_newer_client_is_ready(self):
        self.assertEqual(self._evaluate("0.2.0", "0.2.0"), (0, "MODE=tailscale READY"))
        self.assertEqual(self._evaluate("0.3.0", "0.2.0"), (0, "MODE=tailscale READY"))

    def test_unknown_hub_version_is_ignored(self):
        self.assertEqual(self._evaluate("0.1.0", ""), (0, "MODE=tailscale READY"))

    def test_version_parts_compare_numerically(self):
        code, _ = self._evaluate("0.9.9", "0.10.0")
        self.assertEqual(code, 3)


class HostSelectionTests(unittest.TestCase):
    def _profiles(self, root, entries):
        for name, host, project in entries:
            (root / f"{name}.json").write_text(json.dumps({
                "name": name, "host": host, "project": project,
            }), encoding="utf-8")
        return load_profiles(root)

    def test_explicit_host_always_wins(self):
        profiles = load_profiles(REPO_ROOT / "profiles" / "examples")
        self.assertEqual(resolve_host("dotnet", host="runner-b", profiles=profiles),
                         "runner-b")

    def test_single_profile_infers_the_host(self):
        profiles = load_profiles(REPO_ROOT / "profiles" / "examples")
        self.assertEqual(resolve_host("dotnet", profiles=profiles), "runner-a")
        self.assertEqual(resolve_host("pytest", profiles=profiles), "runner-a")

    def test_platform_filters_by_host_name(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            profiles = self._profiles(root, [
                ("p@runner-a-linux", "runner-a-linux", "p"),
                ("p@runner-b-windows", "runner-b-windows", "p"),
            ])
        self.assertEqual(resolve_host("p", platform="linux", profiles=profiles),
                         "runner-a-linux")
        self.assertEqual(resolve_host("p", platform="windows", profiles=profiles),
                         "runner-b-windows")

    def test_missing_platform_is_an_error(self):
        profiles = load_profiles(REPO_ROOT / "profiles" / "examples")
        with self.assertRaises(ValueError) as ctx:
            resolve_host("dotnet", platform="windows", profiles=profiles)
        self.assertIn("pass --host", str(ctx.exception))

    def test_unknown_project_requires_host(self):
        with self.assertRaises(ValueError):
            resolve_host("other")
        self.assertEqual(resolve_host("other", host="h1"), "h1")

    def test_ambiguous_project_requires_host(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            profiles = self._profiles(root, [
                ("p@h1", "h1", "p"),
                ("p@h2", "h2", "p"),
            ])
        with self.assertRaises(ValueError) as ctx:
            resolve_host("p", profiles=profiles)
        self.assertIn("several hosts", str(ctx.exception))
        self.assertEqual(resolve_host("p", host="h2", profiles=profiles), "h2")

    def test_profile_lookup_order(self):
        from calltable.profiles import Profile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            profiles = self._profiles(root, [("p@h1", "h1", "p")])
        # Profile names with `*` cannot be file names on Windows, so add them
        # directly; on POSIX the loader reads exactly the same shape.
        profiles["p@*"] = Profile.from_dict(
            {"name": "p@*", "host": "*", "project": "p"}, "p@*")
        profiles["*@h2"] = Profile.from_dict(
            {"name": "*@h2", "host": "h2", "project": "*"}, "*@h2")
        self.assertEqual(find_profile(profiles, "p", "h1").name, "p@h1")
        self.assertEqual(find_profile(profiles, "p", "h2").name, "p@*")
        self.assertEqual(find_profile(profiles, "other", "h2").name, "*@h2")
        self.assertIsNone(find_profile(profiles, "other", "h9"))


class SubmissionPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@e")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.head)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_tree_uses_head_without_snapshot(self):
        plan = cli.prepare_submission(self.repo)
        self.assertEqual(plan["sha"], self.head)
        self.assertFalse(plan["dirty"])
        self.assertEqual(plan["base_sha"], self.head)
        self.assertEqual(plan["base_ref"], "origin/main")
        self.assertEqual(plan["base_ref_sha"], self.head)

    def test_dirty_tree_snapshots_on_top_of_head(self):
        (self.repo / "new.txt").write_text("n", encoding="utf-8")
        plan = cli.prepare_submission(self.repo)
        self.assertTrue(plan["dirty"])
        self.assertNotEqual(plan["sha"], self.head)
        self.assertEqual(plan["base_sha"], self.head)
        self.assertEqual(git(self.repo, "rev-parse", f"{plan['sha']}^"), self.head)

    def test_no_base_ref(self):
        plan = cli.prepare_submission(self.repo, None)
        self.assertEqual(plan["base_ref"], "")
        self.assertEqual(plan["base_ref_sha"], "")

    def test_missing_base_ref_is_empty(self):
        plan = cli.prepare_submission(self.repo, "origin/nope")
        self.assertEqual(plan["base_ref_sha"], "")

    def test_plan_bundle_carries_missing_base(self):
        base = self.head
        other = git(self.repo, "commit-tree", "HEAD^{tree}", "-p", base,
                    "-m", "other")
        plan = cli.plan_bundle(self.repo, base, other, {base})
        self.assertIsNotNone(plan)
        tips, excludes = plan
        self.assertIn((other, "bases"), tips)
        self.assertEqual(excludes, [])

    def test_plan_bundle_skips_base_already_present(self):
        plan = cli.plan_bundle(self.repo, self.head, self.head, {self.head})
        self.assertIsNone(plan)

    def test_plan_bundle_excludes_common_ancestor(self):
        (self.repo / "b.txt").write_text("b", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "second")
        second = git(self.repo, "rev-parse", "HEAD")
        plan = cli.plan_bundle(self.repo, second, self.head,
                               {self.head})
        tips, excludes = plan
        self.assertEqual(tips, [(second, "snapshots")])
        self.assertEqual(excludes, [self.head])

    def test_newest_common_picks_the_first_present_commit(self):
        from calltable import gitops
        (self.repo / "b.txt").write_text("b", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "second")
        second = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(
            gitops.newest_common(self.repo, second, {self.head}), self.head)
        self.assertEqual(
            gitops.newest_common(self.repo, second, {self.head, second}), second)
        self.assertIsNone(gitops.newest_common(self.repo, second, set()))


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "config", "user.email", "t@e")
        git(self.source, "config", "user.name", "t")
        (self.source / "a.txt").write_text("a", encoding="utf-8")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-qm", "one")
        git(self.source, "branch", "feature")
        git(self.source, "update-ref", "refs/remotes/origin/main",
            git(self.source, "rev-parse", "HEAD"))
        self.mirror_root = self.root / "mirrors"

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_fetches_branches_and_origin_refs(self):
        from calltable import seed as seed_module
        result = seed_module.seed("demo", self.source, self.mirror_root)
        self.assertEqual(result["project"], "demo")
        self.assertTrue(Path(result["mirror"]).exists())
        self.assertGreater(result["objects_after"], 0)
        refs = git(Path(result["mirror"]), "for-each-ref",
                   "--format=%(refname)", "refs/calltable/seed")
        self.assertIn("refs/calltable/seed/heads/main", refs)
        self.assertIn("refs/calltable/seed/heads/feature", refs)
        self.assertIn("refs/calltable/seed/origin/main", refs)
        # 可重复执行
        again = seed_module.seed("demo", self.source, self.mirror_root)
        self.assertGreaterEqual(again["objects_after"], result["objects_after"])

    def test_seed_rejects_bad_inputs(self):
        from calltable import seed as seed_module
        with self.assertRaises(seed_module.SeedError):
            seed_module.seed("../bad", self.source, self.mirror_root)
        with self.assertRaises(seed_module.SeedError):
            seed_module.seed("demo", self.root / "nope", self.mirror_root)

    def test_hub_mirror_root_is_data_mirrors(self):
        from calltable.config import Config
        from calltable.seed import mirror_root_for
        config = Config(path=self.root / "c.json", raw={"token": "t"},
                        token="t", data_dir=self.root / "data")
        self.assertEqual(mirror_root_for(config), self.root / "data" / "mirrors")
        config.raw["exec_host"] = "h1"
        config.raw["mirror_root"] = str(self.root / "exec-mirrors")
        self.assertEqual(mirror_root_for(config), self.root / "exec-mirrors")

    def test_top_level_seed_alias(self):
        cfg = self.root / "hub2.json"
        cfg.write_text(json.dumps({
            "token": "t", "data_dir": str(self.root / "data2"),
            "profiles_dir": str(self.root / "profiles"),
        }), encoding="utf-8")
        code = cli.main(["seed", "--config", str(cfg),
                         "--project", "demo2", "--from", str(self.source)])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "data2" / "mirrors" / "demo2.git" / "HEAD").exists())

    def test_seed_cli_writes_hub_mirror(self):
        cfg = self.root / "hub.json"
        cfg.write_text(json.dumps({
            "token": "t", "data_dir": str(self.root / "data"),
            "profiles_dir": str(self.root / "profiles"),
        }), encoding="utf-8")
        code = cli.main(["cli", "seed", "--config", str(cfg),
                         "--project", "demo", "--from", str(self.source)])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "data" / "mirrors" / "demo.git" / "HEAD").exists())


class BundleWarningTests(unittest.TestCase):
    def test_threshold(self):
        from calltable.cli import SEED_WARN_BYTES, bundle_warning
        self.assertIsNone(bundle_warning(SEED_WARN_BYTES))
        warning = bundle_warning(SEED_WARN_BYTES + 1)
        self.assertIsNotNone(warning)
        self.assertIn("seed", warning)
        self.assertIn("--yes", warning)

    def test_human_bytes(self):
        from calltable.util import human_bytes
        self.assertEqual(human_bytes(512), "512B")
        self.assertEqual(human_bytes(20 * 1024 * 1024), "20.0MB")


class DoctorTests(unittest.TestCase):
    def test_parse_status(self):
        payload = json.dumps({
            "CurrentTailnet": {"Name": "example.ts.net"},
            "Self": {"Online": True},
        })
        status = doctor.parse_tailscale_status(payload)
        self.assertEqual(status["tailnet"], "example.ts.net")
        self.assertTrue(status["online"])
        self.assertTrue(status["ok"])

    def test_parse_status_offline_and_garbage(self):
        status = doctor.parse_tailscale_status(
            json.dumps({"CurrentTailnet": {"Name": "x"}, "Self": {"Online": False}}))
        self.assertFalse(status["online"])
        self.assertFalse(doctor.parse_tailscale_status("not json")["ok"])

    def test_find_tailscale_prefers_path_then_defaults(self):
        with mock.patch("calltable.doctor.shutil.which", return_value="/usr/bin/tailscale"):
            self.assertEqual(doctor.find_tailscale(), "/usr/bin/tailscale")
        # 钉死 is_wsl：在 WSL 上跑时 WSL interop 路径真实存在，会先于 mock 的
        # macOS 路径命中，这条用例测的是非 WSL 的默认路径回退顺序。
        with mock.patch("calltable.doctor.shutil.which", return_value=None), \
                mock.patch("calltable.doctor.is_wsl", return_value=False), \
                mock.patch("calltable.doctor.WINDOWS_TAILSCALE") as win, \
                mock.patch("calltable.doctor.MACOS_TAILSCALE") as mac:
            win.exists.return_value = False
            mac.exists.return_value = True
            mac.__str__ = lambda self: "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
            self.assertTrue(str(doctor.find_tailscale()).endswith("Tailscale"))

    def test_tailscale_candidates_include_wsl_interop(self):
        with mock.patch("calltable.doctor.shutil.which", return_value=None),                 mock.patch("calltable.doctor.is_wsl", return_value=True):
            candidates = doctor.tailscale_candidates()
        self.assertIn(str(doctor.WSL_TAILSCALE), [str(c) for c in candidates])
        self.assertIn("tailscale.exe", [str(c) for c in candidates])

    def _evaluate(self, **overrides):
        base = dict(owner_tailnet="example.ts.net", tailscale_installed=True,
                    tailnet="example.ts.net", online=True, python_ok=True,
                    git_ok=True, git_version_text="2.45.0", hub_ok=True,
                    auth_ok=True, hub_url="http://h:8091", detail="")
        base.update(overrides)
        return doctor.evaluate(**base)

    def test_ready(self):
        code, line = self._evaluate()
        self.assertEqual((code, line), (0, "MODE=tailscale READY"))

    def test_offline_tailscale_is_not_ready(self):
        code, line = self._evaluate(online=False)
        self.assertEqual(code, 3)
        self.assertIn("tailscale up", line)

    def test_not_ready_reasons_and_fix_commands(self):
        code, line = self._evaluate(online=False)
        self.assertEqual(code, 3)
        self.assertIn("MODE=tailscale NOT-READY:", line)
        self.assertIn("tailscale up", line)

        code, line = self._evaluate(hub_ok=False, detail="connection refused")
        self.assertEqual(code, 3)
        self.assertIn("curl http://h:8091/api/health", line)

        code, line = self._evaluate(auth_ok=False, detail="401")
        self.assertEqual(code, 3)
        self.assertIn("ct setup --hub http://h:8091", line)

        code, line = self._evaluate(python_ok=False, git_ok=False,
                                    git_version_text="2.10.0")
        self.assertEqual(code, 3)
        self.assertIn("python", line)
        self.assertIn("git 2.10.0", line)

    def test_not_ready_reason_is_ascii_for_agents(self):
        code, line = self._evaluate(hub_ok=False, detail="拒绝连接")
        self.assertEqual(code, 3)
        line.encode("ascii")  # 不抛异常

    def test_doctor_run_reports_tailscale_details(self):
        with mock.patch("calltable.doctor.find_tailscale", return_value="/usr/bin/tailscale"), \
                mock.patch("calltable.doctor.tailscale_status",
                           return_value={"ok": True, "tailnet": "example.ts.net",
                                         "online": True, "error": ""}), \
                mock.patch("calltable.doctor.check_hub", return_value=(True, True, "")):
            code, line, details = doctor.run(hub_url="http://h:1", token="t",
                                             owner_tailnet="example.ts.net")
        self.assertEqual((code, line), (0, "MODE=tailscale READY"))
        self.assertTrue(any("tailscale" in d for d in details))


class HostsOutputTests(unittest.TestCase):
    def test_hosts_shows_groups_offline_and_per_host_locks(self):
        import argparse
        import contextlib
        import io
        from calltable import cli as cli_module

        state = {
            "groups": {"linux": ["runner-a", "runner-b"]},
            "hosts": [
                {"name": "runner-a", "online": True, "slots": {"heavy": 1},
                 "used": {}, "locks": ["fullgate@runner-a"], "running": []},
                {"name": "runner-b", "online": False, "slots": {"heavy": 1},
                 "used": {}, "locks": [], "running": []},
            ],
            "queued": [],
        }
        fake = mock.Mock()
        fake.get_json.return_value = state
        args = argparse.Namespace(json=False, hub=None, token=None)
        buffer = io.StringIO()
        with mock.patch.object(cli_module, "_client", return_value=fake),                 contextlib.redirect_stdout(buffer):
            cli_module.cmd_hosts(None, args)
        output = buffer.getvalue()
        self.assertIn("group linux", output)
        self.assertIn("runner-a", output)
        self.assertIn("runner-b", output)
        self.assertIn("offline", output)
        self.assertIn("fullgate@runner-a", output)


class DryRunTests(unittest.TestCase):
    """A：提交前打印最终生效的 slot/locks/timeout 与命中的规则。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@e")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a").write_text("a", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.config = make_config(Path(self.tmp.name) / "cfg",
                                  profiles=False,
                                  profiles_dir=str(REPO_ROOT / "profiles"
                                                   / "examples"))
        self.cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def _run(self, *argv):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["cli", "run", "--config",
                             str(self.config.path), *argv])
        return code, buffer.getvalue()

    def test_dry_run_reports_gate_resolution_and_submits_nothing(self):
        code, out = self._run("--project", "pnpm", "--dry-run", "--", "pnpm", "test")
        self.assertEqual(code, 0)
        self.assertIn("dry-run (nothing submitted)", out)
        self.assertIn("slot=heavy", out)
        self.assertIn("locks=pnpm-fullgate@{host}", out)
        self.assertIn("timeout=3600s", out)
        self.assertIn("pnpm", out)

    def test_dry_run_reports_light_default_for_unit_tests(self):
        code, out = self._run("--project", "pnpm", "--dry-run", "--",
                              "pnpm", "test:unit")
        self.assertEqual(code, 0)
        self.assertIn("slot=light", out)
        self.assertIn("locks=-", out)
        self.assertIn("timeout=1800s", out)

    def test_dry_run_honours_explicit_overrides_and_no_lock(self):
        code, out = self._run("--project", "pnpm", "--dry-run",
                              "--slot", "light", "--no-lock", "--timeout", "30m",
                              "--", "pnpm", "test")
        self.assertEqual(code, 0)
        self.assertIn("slot=light", out)
        self.assertIn("locks=-", out)
        self.assertIn("timeout=1800s", out)

    def test_dry_run_does_not_create_a_snapshot_commit(self):
        before = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "a").write_text("dirty", encoding="utf-8")
        self._run("--project", "pnpm", "--dry-run", "--", "pnpm", "test")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)


class DryRunOutsideGitTests(unittest.TestCase):
    def test_dry_run_works_without_a_git_repository(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            config = make_config(Path(tmp) / "cfg", profiles=False,
                                 profiles_dir=str(REPO_ROOT / "profiles"
                                                  / "examples"))
            cwd = os.getcwd()
            os.chdir(plain)
            try:
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer),                         contextlib.redirect_stderr(io.StringIO()):
                    code = cli.main(["cli", "run", "--config",
                                     str(config.path), "--project", "pnpm",
                                     "--dry-run", "--", "pnpm", "test"])
            finally:
                os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertIn("slot=heavy", buffer.getvalue())
        self.assertIn("pnpm-fullgate@{host}", buffer.getvalue())


class HostsQueueVisibilityTests(unittest.TestCase):
    def test_hosts_shows_disk_memory_queue_and_disk_low(self):
        import argparse
        import contextlib
        import io
        from calltable import cli as cli_module

        state = {
            "groups": {"linux": ["runner-a", "runner-b"]},
            "group_queue": {"linux": 1},
            "hosts": [
                {"name": "runner-a", "online": True, "slots": {"heavy": 1},
                 "used": {"heavy": 1}, "locks": [], "running": [], "queue": 2,
                 "disk_low": True,
                 "system": {"disk_free_gb": 12.5, "disk_runs_gb": 310.0,
                            "mem_free_gb": 3.1, "mem_total_gb": 31.0,
                            "load1": 4.2,
                            "disk_reason": "runs 310.0GB > 300GB"}},
                {"name": "runner-b", "online": True, "slots": {"light": 2},
                 "used": {}, "locks": [], "running": [], "queue": 0,
                 "system": {"disk_free_gb": 500.0, "disk_runs_gb": 20.0,
                            "mem_free_gb": 30.0, "mem_total_gb": 60.0,
                            "load1": 1.0}},
            ],
            "queued": [{"id": "job-1", "project": "demo", "host": "runner-a",
                        "slot": "heavy", "locks": [], "label": "",
                        "queued_seconds": 90, "ahead": 1}],
        }
        fake = mock.Mock()
        fake.get_json.return_value = state
        args = argparse.Namespace(json=False, hub=None, token=None)
        buffer = io.StringIO()
        with mock.patch.object(cli_module, "_client", return_value=fake),                 contextlib.redirect_stdout(buffer):
            cli_module.cmd_hosts(None, args)
        out = buffer.getvalue()
        self.assertIn("DISK_LOW", out)
        self.assertIn("disk_free=12.5G", out)
        self.assertIn("runs=310.0G", out)
        self.assertIn("mem=3.1G/31.0G", out)
        self.assertIn("load=4.2", out)
        self.assertIn("queue=2", out)
        self.assertIn("queued=1", out)
        self.assertIn("waited=1m30s", out)
        self.assertIn("ahead=1", out)


class SetupTests(unittest.TestCase):
    def test_token_order_explicit_then_file_then_env(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("file-token", encoding="utf-8")
            self.assertEqual(setup_module.resolve_token("explicit", {}, None),
                             "explicit")
            self.assertEqual(
                setup_module.resolve_token(None, {"CALLTABLE_TOKEN": "env"}, None,
                                           token_file=str(token_file)),
                "file-token")
            self.assertEqual(
                setup_module.resolve_token(None, {"CALLTABLE_TOKEN": "env"}, None),
                "env")

    def test_missing_token_is_actionable(self):
        with self.assertRaises(setup_module.SetupError) as ctx:
            setup_module.resolve_token(None, {}, None)
        message = str(ctx.exception)
        self.assertIn("--token", message)
        self.assertIn("CALLTABLE_TOKEN", message)

    def test_no_implicit_ssh_or_remote_read(self):
        # Regression: setup must never read a token from a hardcoded host/path.
        self.assertFalse(hasattr(setup_module, "SSH_TOKEN_CMD"))
        self.assertFalse(hasattr(setup_module, "TOKEN_PATH"))

    def test_token_file_missing_is_actionable(self):
        with self.assertRaises(setup_module.SetupError) as ctx:
            setup_module.resolve_token(None, {}, None,
                                       token_file="/nonexistent/token")
        self.assertIn("--token-file", str(ctx.exception))
        self.assertIn("--token", str(ctx.exception))

    def test_setup_cli_accepts_token_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("cli-token", encoding="utf-8")
            target = Path(tmp) / "cfg.json"
            code = cli.main(["cli", "setup", "--hub", "http://hub:1",
                             "--token-file", str(token_file),
                             "--config", str(target)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(target.read_text())["token"], "cli-token")

    def test_setup_writes_config_with_restricted_permissions(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / ".config" / "calltable" / "config.json"
            path, data = setup_module.setup(
                hub="http://hub:8091", token="tok", path=target)
            self.assertTrue(path.exists())
            self.assertEqual(data["hub_url"], "http://hub:8091")
            self.assertEqual(data["token"], "tok")
            self.assertNotIn("owner_tailnet", data)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            else:
                acl = subprocess.run(["icacls", str(path)], capture_output=True,
                                     text=True, encoding="utf-8", errors="replace")
                user = os.environ.get("USERNAME", "")
                self.assertIn(user.lower(), acl.stdout.lower())

    def test_setup_stores_owner_tailnet_only_when_given(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config.json"
            _, data = setup_module.setup(hub="http://hub:1", token="t",
                                         owner_tailnet="example.ts.net",
                                         path=target)
        self.assertEqual(data["owner_tailnet"], "example.ts.net")

    def test_setup_merges_existing_config(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "config.json"
            target.write_text(json.dumps({"keep": 1, "hub_url": "old"}),
                              encoding="utf-8")
            _, data = setup_module.setup(hub="http://new:1", token="t", path=target)
        self.assertEqual(data["keep"], 1)
        self.assertEqual(data["hub_url"], "http://new:1")

    def test_self_update_uses_git_pull(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Already up to date.", stderr=""))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            (Path(tmp) / ".git").mkdir()
            code, output = setup_module.self_update(tmp, runner=runner)
        self.assertEqual(code, 0)
        self.assertIn("Already up to date", output)
        self.assertEqual(runner.call_args.args[0][:4], ["git", "-C", tmp, "pull"])


class PlatformImportTests(unittest.TestCase):
    """macOS 只用客户端：import 路径不能碰到 Windows/Linux 专属实现。"""

    def test_proc_does_not_eagerly_import_platform_modules(self):
        # 用子进程验证，避免污染本进程的 sys.modules（会打乱别处的 mock.patch）。
        script = (
            "import sys, calltable.proc;"
            "print('win=' + str('calltable.proc_windows' in sys.modules));"
            "print('linux=' + str('calltable.proc_linux' in sys.modules))"
        )
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("win=False", proc.stdout)
        self.assertIn("linux=False", proc.stdout)

    def test_client_modules_import_without_platform_side_effects(self):
        import importlib

        for name in ("calltable.cli", "calltable.client", "calltable.config",
                     "calltable.doctor", "calltable.setup", "calltable.profiles",
                     "calltable.util", "calltable.gitops", "calltable.dashboard",
                     "calltable.db", "calltable.hub"):
            importlib.import_module(name)


if __name__ == "__main__":
    unittest.main()
