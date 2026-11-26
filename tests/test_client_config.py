"""本机配置位置（~/.calltable/）、machines 登记，以及脱敏的机器可读视图
``ct config show --json`` / ``ct doctor --json``（契约：spec/client-config.md）。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from calltable import cli, client_view, setup as setup_module
from calltable.config import (ConfigError, legacy_user_config_path, load_config,
                              locate_config, parse_machines, user_config_path)

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "spec" / "client-config.md"
TOKEN = "tok-SECRET-0123456789abcdef"
LEGACY_TOKEN = "tok-LEGACY-9876543210fedcba"

STATE = {
    "hosts": [
        {"name": "runner-a", "online": True, "draining": False, "disk_low": False,
         "queue": 2, "os": "linux", "labels": ["linux"], "system": {}},
        {"name": "runner-b", "online": False, "draining": True, "disk_low": True,
         "queue": 0, "os": "windows", "labels": [], "system": {}},
    ],
    "groups": {"burst": ["runner-a", "runner-b"]},
}


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def flatten_keys(value, prefix: str = "", leaves=("groups",)) -> set[str]:
    """JSON 对象的字段路径：``a.b``；对象数组元素写作 ``a[].b``。"""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            keys.add(path)
            if path not in leaves:
                keys |= flatten_keys(item, path, leaves)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                keys |= flatten_keys(item, f"{prefix}[]", leaves)
    return keys


def spec_fields(heading: str) -> set[str]:
    """契约文档里某一节字段表第一列（反引号里的字段路径）。"""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    section = text[start:end if end != -1 else len(text)]
    return set(re.findall(r"^\|\s*`([^`]+)`\s*\|", section, flags=re.M))


class HomeTestCase(unittest.TestCase):
    """一个假的 home，清掉 CALLTABLE_CONFIG / CALLTABLE_TOKEN。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.new_cfg = self.home / ".calltable" / "config.json"
        self.old_cfg = self.home / ".config" / "calltable" / "config.json"
        patches = [mock.patch("pathlib.Path.home", return_value=self.home),
                   mock.patch.dict(os.environ, {}, clear=False)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("CALLTABLE_CONFIG", None)
        os.environ.pop("CALLTABLE_TOKEN", None)
        self.addCleanup(self.tmp.cleanup)

    def write(self, path: Path, data: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def same(self, left, right) -> bool:
        return Path(left).resolve() == Path(right).resolve()


class ConfigLocationTests(HomeTestCase):
    def test_user_paths(self):
        self.assertEqual(user_config_path(), self.new_cfg)
        self.assertEqual(legacy_user_config_path(), self.old_cfg)

    def test_only_new_location(self):
        self.write(self.new_cfg, {"token": TOKEN})
        location = locate_config(None)
        self.assertEqual((location.path, location.source, location.shadowed),
                         (self.new_cfg, "home", []))
        config = load_config(None)
        self.assertEqual(config.source, "home")
        self.assertEqual(config.token, TOKEN)

    def test_only_old_location_is_unchanged(self):
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "hub_url": "http://old:1"})
        config = load_config(None)
        self.assertTrue(self.same(config.path, self.old_cfg))
        self.assertEqual(config.source, "legacy")
        self.assertEqual(config.shadowed, [])
        self.assertEqual(config.hub_url, "http://old:1")

    def test_both_locations_new_wins_and_old_is_shadowed(self):
        self.write(self.new_cfg, {"token": TOKEN, "hub_url": "http://new:1"})
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "hub_url": "http://old:1"})
        config = load_config(None)
        self.assertTrue(self.same(config.path, self.new_cfg))
        self.assertEqual(config.source, "home")
        self.assertEqual(config.shadowed, [self.old_cfg])
        self.assertEqual(config.hub_url, "http://new:1")

    def test_flag_and_env_still_come_first(self):
        self.write(self.new_cfg, {"token": TOKEN})
        explicit = self.write(self.home / "explicit.json", {"token": TOKEN})
        env = self.write(self.home / "env.json", {"token": TOKEN})
        self.assertEqual(locate_config(str(explicit)).source, "flag")
        with mock.patch.dict(os.environ, {"CALLTABLE_CONFIG": str(env)}):
            location = locate_config(None)
            self.assertEqual((location.path, location.source), (env, "env"))
            self.assertEqual(locate_config(str(explicit)).source, "flag")

    def test_missing_everything_mentions_setup(self):
        with self.assertRaises(ConfigError) as ctx:
            locate_config(None)
        self.assertIn("ct setup", str(ctx.exception))


    def test_directory_at_new_location_falls_back_to_legacy(self):
        self.new_cfg.mkdir(parents=True)
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "hub_url": "http://old:1"})
        location = locate_config(None)
        self.assertEqual((location.source, location.shadowed), ("legacy", []))
        self.assertTrue(self.same(location.path, self.old_cfg))
        self.assertEqual(load_config(None).token, LEGACY_TOKEN)

    def test_empty_file_at_new_location_still_fails_loudly(self):
        self.new_cfg.parent.mkdir(parents=True)
        self.new_cfg.write_text("", encoding="utf-8")
        self.write(self.old_cfg, {"token": LEGACY_TOKEN})
        with self.assertRaises(ConfigError) as ctx:
            load_config(None)
        self.assertIn(str(self.new_cfg.resolve()), str(ctx.exception))


class SetupLocationTests(HomeTestCase):
    def assert_owner_only(self, path: Path):
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        else:
            acl = subprocess.run(["icacls", str(path)], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace")
            self.assertIn(os.environ.get("USERNAME", "").lower(),
                          acl.stdout.lower())

    def test_new_install_writes_the_new_location(self):
        code, out, _ = run_cli(["cli", "setup", "--hub", "http://hub:1",
                                "--token", TOKEN])
        self.assertEqual(code, 0)
        self.assertTrue(self.new_cfg.exists())
        self.assertFalse(self.old_cfg.exists())
        self.assertIn(str(self.new_cfg), out)
        self.assertEqual(json.loads(self.new_cfg.read_text())["token"], TOKEN)
        self.assert_owner_only(self.new_cfg)

    def test_existing_legacy_config_is_updated_in_place(self):
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "keep": 1})
        path, data = setup_module.setup(hub="http://hub:2", token=TOKEN)
        self.assertEqual(path, self.old_cfg)
        self.assertFalse(self.new_cfg.exists())
        self.assertEqual((data["keep"], data["token"]), (1, TOKEN))
        self.assert_owner_only(self.old_cfg)

    def test_new_location_wins_when_both_exist(self):
        self.write(self.new_cfg, {"token": "x", "machines": {"m1": {}}})
        self.write(self.old_cfg, {"token": LEGACY_TOKEN})
        path, data = setup_module.setup(hub="http://hub:3", token=TOKEN)
        self.assertEqual(path, self.new_cfg)
        self.assertEqual(data["machines"], {"m1": {}})
        self.assertEqual(json.loads(self.old_cfg.read_text())["token"], LEGACY_TOKEN)


    def test_directory_at_new_location_updates_legacy_file(self):
        self.new_cfg.mkdir(parents=True)
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "keep": 1})
        path, data = setup_module.setup(hub="http://hub:4", token=TOKEN)
        self.assertEqual(path, self.old_cfg)
        self.assertEqual((data["keep"], data["token"]), (1, TOKEN))
        self.assertTrue(self.new_cfg.is_dir())

    def test_unwritable_target_is_a_setup_error(self):
        self.new_cfg.mkdir(parents=True)
        with self.assertRaises(setup_module.SetupError) as ctx:
            setup_module.setup(hub="http://hub:5", token=TOKEN)
        self.assertIn(str(self.new_cfg), str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))
        code, out, err = run_cli(["cli", "setup", "--hub", "http://hub:5",
                                  "--token", TOKEN])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("setup failed: cannot write config", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(TOKEN, err)


@unittest.skipIf(os.name == "nt", "POSIX permissions")
class SetupPermissionTests(HomeTestCase):
    def mode(self, path: Path) -> int:
        return path.stat().st_mode & 0o777

    def test_existing_calltable_directory_keeps_its_mode(self):
        data_dir = self.new_cfg.parent
        data_dir.mkdir()
        os.chmod(data_dir, 0o755)
        setup_module.setup(hub="http://hub:1", token=TOKEN)
        self.assertEqual(self.mode(data_dir), 0o755)
        self.assertEqual(self.mode(self.new_cfg), 0o600)

    def test_directory_created_by_setup_is_owner_only(self):
        setup_module.setup(hub="http://hub:1", token=TOKEN)
        self.assertEqual(self.mode(self.new_cfg.parent), 0o700)
        self.assertEqual(self.mode(self.new_cfg), 0o600)

    def test_legacy_directory_is_still_restricted(self):
        self.write(self.old_cfg, {"token": LEGACY_TOKEN})
        os.chmod(self.old_cfg.parent, 0o755)
        setup_module.setup(hub="http://hub:1", token=TOKEN)
        self.assertEqual(self.mode(self.old_cfg.parent), 0o700)
        self.assertEqual(self.mode(self.old_cfg), 0o600)

    def test_explicit_path_in_existing_directory_keeps_its_mode(self):
        target_dir = self.home / "elsewhere"
        target_dir.mkdir()
        os.chmod(target_dir, 0o755)
        setup_module.setup(hub="http://hub:1", token=TOKEN,
                           path=target_dir / "config.json")
        self.assertEqual(self.mode(target_dir), 0o755)
        self.assertEqual(self.mode(target_dir / "config.json"), 0o600)

    def test_new_file_and_directory_are_created_owner_only(self):
        # 宽松的 umask 下、且之后的 chmod 全都不生效时，文件和目录仍是
        # 0600 / 0700：说明它们一创建就是这个权限，没有可读的空窗。
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)
        with mock.patch("os.chmod"), mock.patch("os.fchmod"):
            setup_module.setup(hub="http://hub:1", token=TOKEN)
        self.assertEqual(self.mode(self.new_cfg.parent), 0o700)
        self.assertEqual(self.mode(self.new_cfg), 0o600)

    def test_existing_file_is_tightened_to_0600(self):
        target = self.write(self.home / "cfg.json", {"keep": 1})
        os.chmod(target, 0o644)
        _, data = setup_module.setup(hub="http://hub:1", token=TOKEN, path=target)
        self.assertEqual(data["keep"], 1)
        self.assertEqual(self.mode(target), 0o600)


class MachinesTests(unittest.TestCase):
    def test_absent_or_null_is_empty(self):
        self.assertEqual(parse_machines(None), [])
        self.assertEqual(parse_machines({}), [])

    def test_valid_entries_are_normalised_in_order(self):
        machines = parse_machines({
            "runner-b": {"ssh": "rb", "platform": "Windows", "purpose": " ui tests "},
            "runner-a": {"platform": None},
            "mac.mini": None,
        })
        self.assertEqual(machines, [
            {"name": "runner-b", "ssh": "rb", "platform": "windows",
             "purpose": "ui tests"},
            {"name": "runner-a", "ssh": "", "platform": "", "purpose": ""},
            {"name": "mac.mini", "ssh": "", "platform": "", "purpose": ""},
        ])

    def test_invalid_shapes_are_rejected(self):
        bad = [
            ["runner-a"],
            {"runner-a": "rb"},
            {"-bad": {}},
            {"has space": {}},
            {"runner-a": {"host": "10.0.0.1"}},
            {"runner-a": {"identity_file": "~/.ssh/id"}},
            {"runner-a": {"ssh": 22}},
            {"runner-a": {"ssh": "user@host -p 22"}},
            {"runner\n": {}},
            {"runner-a\n": {"ssh": "runner-a"}},
            {"runner-a": {"ssh": "-oProxyCommand=touch${IFS}/tmp/x"}},
            {"runner-a": {"ssh": "-p"}},
            {"runner-a": {"ssh": "user@-host"}},
            {"runner-a": {"ssh": "host;id"}},
            {"runner-a": {"platform": "solaris"}},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_machines(raw)

    def test_ordinary_ssh_aliases_are_accepted(self):
        for alias in ("host", "user@host", "host.example", "runner-a",
                      "runner_b", "build-host_2.example", "_alias"):
            with self.subTest(alias=alias):
                self.assertEqual(parse_machines({"m": {"ssh": alias}})[0]["ssh"],
                                 alias)

    def test_load_config_rejects_bad_machines(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"token": TOKEN,
                                        "machines": {"x": {"port": 22}}}),
                            encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(str(path))
            self.assertIn("machines.x", str(ctx.exception))
            self.assertNotIn(TOKEN, str(ctx.exception))


class ConfigShowTests(HomeTestCase):
    MACHINES = {"runner-a": {"ssh": "runner-a", "platform": "linux",
                             "purpose": "Linux builds"}}

    def show(self, *extra) -> tuple[int, dict, str]:
        code, out, err = run_cli(["cli", "config", "show", "--json", *extra])
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn(LEGACY_TOKEN, out + err)
        return code, json.loads(out), out

    def test_every_documented_field_is_emitted(self):
        self.write(self.new_cfg, {"token": TOKEN, "hub_url": "http://hub:8091",
                                  "owner_tailnet": "example.ts.net",
                                  "machines": self.MACHINES})
        self.write(self.old_cfg, {"token": LEGACY_TOKEN})
        code, payload, _ = self.show()
        self.assertEqual(code, 0)
        self.assertEqual(flatten_keys(payload),
                         spec_fields("## 3. `ct config show --json`"))
        self.assertEqual(payload["schema_version"], client_view.SCHEMA_VERSION)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["kind"], "calltable.config")
        self.assertEqual(payload["client_version"], cli.__version__)
        block = payload["config"]
        self.assertTrue(self.same(block["path"], self.new_cfg))
        self.assertEqual(block["source"], "home")
        self.assertEqual(len(block["shadowed"]), 1)
        self.assertTrue(self.same(block["shadowed"][0], self.old_cfg))
        self.assertIsNone(block["error"])
        self.assertEqual([e["source"] for e in block["search_order"]],
                         ["home", "legacy"])
        self.assertEqual([e["exists"] for e in block["search_order"]], [True, True])
        self.assertTrue(self.same(block["search_order"][1]["path"], self.old_cfg))
        self.assertEqual(payload["hub_url"], "http://hub:8091")
        self.assertIs(payload["token_present"], True)
        self.assertEqual(payload["token_source"], "config")
        self.assertEqual(payload["owner_tailnet"], "example.ts.net")
        self.assertEqual(payload["machines"], [
            {"name": "runner-a", "ssh": "runner-a", "platform": "linux",
             "purpose": "Linux builds"}])

    def test_only_legacy_is_reported_as_legacy(self):
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "hub_url": "http://old:1"})
        code, payload, _ = self.show()
        self.assertEqual(code, 0)
        self.assertEqual(payload["config"]["source"], "legacy")
        self.assertEqual(payload["config"]["shadowed"], [])
        self.assertEqual(payload["hub_url"], "http://old:1")
        self.assertEqual(payload["machines"], [])

    def test_directory_at_new_location_reports_legacy(self):
        self.new_cfg.mkdir(parents=True)
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "hub_url": "http://old:1"})
        code, payload, _ = self.show()
        self.assertEqual(code, 0)
        self.assertEqual(payload["config"]["source"], "legacy")
        self.assertTrue(self.same(payload["config"]["path"], self.old_cfg))
        self.assertEqual(payload["config"]["shadowed"], [])
        self.assertIsNone(payload["config"]["error"])
        self.assertEqual([e["exists"] for e in payload["config"]["search_order"]],
                         [False, True])
        self.assertEqual(payload["hub_url"], "http://old:1")

    def test_flag_and_env_sources(self):
        explicit = self.write(self.home / "x.json", {"token": TOKEN})
        code, payload, _ = self.show("--config", str(explicit))
        self.assertEqual((code, payload["config"]["source"]), (0, "flag"))
        self.assertEqual(payload["config"]["search_order"][0],
                         {"source": "flag", "path": str(explicit),
                          "exists": True})
        with mock.patch.dict(os.environ, {"CALLTABLE_CONFIG": str(explicit)}):
            code, payload, _ = self.show()
        self.assertEqual((code, payload["config"]["source"]), (0, "env"))

    def test_token_from_environment_and_missing_token(self):
        self.write(self.new_cfg, {"hub_url": "http://hub:1"})
        code, payload, _ = self.show()
        self.assertEqual(code, 0)
        self.assertIs(payload["token_present"], False)
        self.assertEqual(payload["token_source"], "none")
        with mock.patch.dict(os.environ, {"CALLTABLE_TOKEN": TOKEN}):
            code, payload, _ = self.show()
        self.assertIs(payload["token_present"], True)
        self.assertEqual(payload["token_source"], "env")

    def test_no_config_exits_2_with_error(self):
        code, payload, _ = self.show()
        self.assertEqual(code, 2)
        self.assertIsNone(payload["config"]["path"])
        self.assertIsNone(payload["config"]["source"])
        self.assertIn("ct setup", payload["config"]["error"])
        self.assertIsNone(payload["hub_url"])
        self.assertIs(payload["token_present"], False)
        self.assertEqual(payload["machines"], [])

    def test_invalid_config_reports_path_and_error(self):
        self.write(self.new_cfg, {"token": TOKEN,
                                  "machines": {"x": {"host": "10.0.0.1"}}})
        code, payload, _ = self.show()
        self.assertEqual(code, 2)
        self.assertTrue(self.same(payload["config"]["path"], self.new_cfg))
        self.assertEqual(payload["config"]["source"], "home")
        self.assertIn("machines.x", payload["config"]["error"])

    def test_hub_url_credentials_are_redacted(self):
        self.write(self.new_cfg, {
            "token": TOKEN,
            "hub_url": f"https://user:{LEGACY_TOKEN}@hub.example:8443/x?token={TOKEN}"})
        code, payload, raw = self.show()
        self.assertEqual(payload["hub_url"], "https://hub.example:8443/x")

    def test_token_never_appears_even_when_embedded(self):
        # 兜底：令牌被写进了别的字段（比如用途说明）也会被替换掉。
        self.write(self.new_cfg, {"token": TOKEN, "machines": {
            "m": {"purpose": f"do not paste {TOKEN} here"}}})
        code, payload, _ = self.show()
        self.assertEqual(payload["machines"][0]["purpose"],
                         f"do not paste {client_view.REDACTED} here")

    def test_text_output_has_no_token(self):
        self.write(self.new_cfg, {"token": TOKEN, "machines": self.MACHINES})
        self.write(self.old_cfg, {"token": LEGACY_TOKEN})
        code, out, err = run_cli(["cli", "config", "show"])
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out + err)
        self.assertIn("source=home", out)
        self.assertIn("ignored", out)
        self.assertIn("runner-a", out)


class DoctorJsonTests(HomeTestCase):
    def probes(self, *, hub_ok=True, auth_ok=True, detail="", state=STATE):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("calltable.doctor.find_tailscale",
                                       return_value=None))
        stack.enter_context(mock.patch("calltable.doctor.git_version",
                                       return_value=(2, 45, 0)))
        self.check_hub = stack.enter_context(mock.patch(
            "calltable.doctor.check_hub", return_value=(hub_ok, auth_ok, detail)))
        stack.enter_context(mock.patch(
            "calltable.doctor.hub_health",
            return_value=(hub_ok, "0.3.0" if hub_ok else "", detail)))
        stack.enter_context(mock.patch(
            "calltable.doctor.fetch_state",
            return_value=(state, "") if state is not None else (None, "boom")))
        self.addCleanup(stack.close)

    def doctor_json(self, *extra) -> tuple[int, dict]:
        code, out, err = run_cli(["cli", "doctor", "--json", *extra])
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn(LEGACY_TOKEN, out + err)
        return code, json.loads(out)

    def test_only_new_location_is_ready(self):
        self.write(self.new_cfg, {"token": TOKEN, "hub_url": "http://hub:1"})
        self.probes()
        code, out, err = run_cli(["cli", "doctor"])
        self.assertEqual((code, out.strip()), (0, "MODE=direct READY"))
        self.assertEqual(self.check_hub.call_args.args, ("http://hub:1", TOKEN))
        self.assertIn("source=home", err)
        self.assertNotIn(TOKEN, out + err)

    def test_only_old_location_behaves_as_before(self):
        self.write(self.old_cfg, {"token": LEGACY_TOKEN, "hub_url": "http://old:1"})
        self.probes()
        code, out, _ = run_cli(["cli", "doctor"])
        self.assertEqual((code, out.strip()), (0, "MODE=direct READY"))
        self.assertEqual(self.check_hub.call_args.args,
                         ("http://old:1", LEGACY_TOKEN))

    def test_ready_payload_covers_every_documented_field(self):
        self.write(self.new_cfg, {"token": TOKEN, "hub_url": "http://hub:1"})
        self.write(self.old_cfg, {"token": LEGACY_TOKEN})
        self.probes()
        code, payload = self.doctor_json()
        self.assertEqual(code, 0)
        self.assertEqual(flatten_keys(payload),
                         spec_fields("## 4. `ct doctor --json`"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["kind"], "calltable.doctor")
        self.assertEqual(payload["client_version"], cli.__version__)
        self.assertEqual(payload["mode"], "direct")
        self.assertIs(payload["ready"], True)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["line"], "MODE=direct READY")
        self.assertEqual(payload["reasons"], [])
        self.assertEqual(payload["config"]["source"], "home")
        self.assertEqual(len(payload["config"]["shadowed"]), 1)
        self.assertIsNone(payload["config"]["error"])
        self.assertIs(payload["token_present"], True)
        self.assertEqual(payload["token_source"], "config")
        self.assertEqual(payload["hub"], {"url": "http://hub:1", "reachable": True,
                                          "auth_ok": True, "version": "0.3.0",
                                          "detail": ""})
        checks = payload["checks"]
        self.assertIs(checks["python"]["ok"], True)
        self.assertTrue(checks["python"]["version"])
        self.assertEqual(checks["git"], {"version": "2.45.0", "ok": True})
        self.assertEqual(checks["tailscale"]["found"], False)
        self.assertEqual(checks["tailscale"]["ok"], False)
        self.assertEqual(checks["tailscale"]["owner_tailnet"], "")
        self.assertEqual(payload["hosts"], [
            {"name": "runner-a", "online": True, "draining": False,
             "disk_low": False, "queue": 2, "os": "linux", "labels": ["linux"]},
            {"name": "runner-b", "online": False, "draining": True,
             "disk_low": True, "queue": 0, "os": "windows", "labels": []},
        ])
        self.assertIsNone(payload["hosts_error"])
        self.assertEqual(payload["groups"], {"burst": ["runner-a", "runner-b"]})

    def test_not_ready_keeps_exit_code_and_lists_reasons(self):
        self.write(self.new_cfg, {"token": TOKEN, "hub_url": "http://hub:1"})
        self.probes(hub_ok=False, auth_ok=False,
                    detail=f"connection refused {TOKEN}")
        code, payload = self.doctor_json()
        text_code, text_out, _ = run_cli(["cli", "doctor"])
        self.assertEqual(code, 3)
        self.assertEqual(text_code, 3)
        self.assertEqual(payload["exit_code"], 3)
        self.assertIs(payload["ready"], False)
        self.assertEqual(payload["line"], text_out.strip().replace(
            TOKEN, client_view.REDACTED))
        self.assertTrue(payload["line"].startswith("MODE=direct NOT-READY: "))
        self.assertEqual(len(payload["reasons"]), 1)
        self.assertIn("unreachable", payload["reasons"][0])
        self.assertIs(payload["hub"]["reachable"], False)
        self.assertIsNone(payload["hosts"])
        self.assertTrue(payload["hosts_error"])

    def test_hub_url_secrets_never_appear_in_json(self):
        cases = [
            ("http://SECRETUSERTOKEN@127.0.0.1:9/?k=abc",
             ["SECRETUSERTOKEN", "abc"]),
            ("http://ops:p%40ss%2Fw0rd@127.0.0.1:9/?x",
             ["p%40ss%2Fw0rd", "p@ss/w0rd", "%40", "w0rd"]),
            ("http://127.0.0.1:9/?k=zq7", ["zq7", "?k"]),
        ]
        shown = "http://127.0.0.1:9/"
        for url, hidden in cases:
            with self.subTest(url=url):
                self.write(self.new_cfg, {"token": TOKEN, "hub_url": url})
                self.probes(hub_ok=False, auth_ok=False,
                            detail=f"refused by {url}")
                code, out, err = run_cli(["cli", "doctor", "--json"])
                self.assertEqual(code, 3)
                for secret in hidden:
                    self.assertNotIn(secret, out)
                payload = json.loads(out)
                self.assertEqual(payload["hub"]["url"], shown)
                self.assertIn(f"hub {shown} unreachable", payload["reasons"][0])
                self.assertIn(f"`curl {shown}/api/health`", payload["reasons"][0])
                self.assertIn(f"hub {shown} unreachable", payload["line"])
                self.assertIn(shown, payload["hub"]["detail"])
                # 文本模式保持原样（脚本会解析这一行）。
                _, text_out, _ = run_cli(["cli", "doctor"])
                self.assertIn(f"hub {url} unreachable", text_out)
                self.assertIn(f"`curl {url}/api/health`", text_out)

    def test_hosts_unavailable_is_reported(self):
        self.write(self.new_cfg, {"token": TOKEN, "hub_url": "http://hub:1"})
        self.probes(state=None)
        code, payload = self.doctor_json()
        self.assertEqual(code, 0)
        self.assertIsNone(payload["hosts"])
        self.assertEqual(payload["hosts_error"], "boom")

    def test_no_config_still_prints_json(self):
        self.probes(hub_ok=False, auth_ok=False, detail="refused")
        code, payload = self.doctor_json()
        self.assertEqual(code, 3)
        self.assertIsNone(payload["config"]["path"])
        self.assertIn("ct setup", payload["config"]["error"])
        self.assertIs(payload["token_present"], False)
        self.assertEqual(payload["token_source"], "none")

    def test_token_flag_and_env_never_printed(self):
        self.write(self.new_cfg, {"hub_url": "http://hub:1"})
        self.probes()
        code, payload = self.doctor_json("--token", TOKEN)
        self.assertEqual(code, 0)
        self.assertEqual(payload["token_source"], "flag")
        with mock.patch.dict(os.environ, {"CALLTABLE_TOKEN": LEGACY_TOKEN}):
            code, payload = self.doctor_json()
        self.assertEqual(payload["token_source"], "env")
        self.assertIs(payload["token_present"], True)


class SpecDocumentTests(unittest.TestCase):
    def test_spec_states_the_schema_version(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn(f"schema_version: {client_view.SCHEMA_VERSION}", text)

    def test_spec_documents_the_config_file_fields(self):
        fields = spec_fields("## 2. Local config file")
        for name in ("hub_url", "token", "owner_tailnet", "machines",
                     "machines.<name>.ssh", "machines.<name>.platform",
                     "machines.<name>.purpose"):
            self.assertIn(name, fields)


if __name__ == "__main__":
    unittest.main()
