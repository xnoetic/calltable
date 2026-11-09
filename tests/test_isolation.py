"""Tests must never read or write the developer's real home directory.

If ``tests/__init__`` ever stops isolating the environment, this module is the
canary: it asserts that the home directory, the user config path and the CLI
environment (including subprocesses) all point at the throwaway test home.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from calltable import config
from calltable.config import ConfigError
from tests import isolation


class IsolationGuardTests(unittest.TestCase):
    def test_home_is_the_temporary_test_home(self):
        self.assertEqual(Path.home().resolve(), isolation.TEST_HOME)
        self.assertNotEqual(isolation.TEST_HOME, isolation.REAL_HOME)

    def test_user_config_and_data_paths_are_isolated(self):
        for path in (config.user_config_path(), config.user_data_dir()):
            self.assertTrue(isolation.is_under(path, isolation.TEST_HOME),
                            f"{path} is not under the test home")
            self.assertFalse(
                isolation.is_under(path, isolation.REAL_HOME)
                and not isolation.is_under(path, isolation.TEST_HOME),
                f"{path} points into the real home")

    def test_guard_rejects_a_real_home_config_path(self):
        real = isolation.REAL_HOME / ".config" / "calltable" / "config.json"
        with self.assertRaises(AssertionError):
            isolation.check_path(real)

    def test_guard_catches_a_real_home_resolution(self):
        # 模拟「测试忘了隔离 / 又改回真实 HOME」：守卫必须直接失败。
        with mock.patch("pathlib.Path.home", return_value=isolation.REAL_HOME):
            with self.assertRaises(AssertionError):
                config.user_config_path()
            with self.assertRaises(AssertionError):
                config.user_data_dir()

    def test_no_implicit_config_is_loadable(self):
        env_path = os.environ.get("CALLTABLE_CONFIG", "")
        self.assertTrue(env_path, "CALLTABLE_CONFIG must point at a test path")
        self.assertTrue(isolation.is_under(env_path, isolation.TEST_HOME))
        self.assertFalse(Path(env_path).exists())
        with self.assertRaises(ConfigError) as ctx:
            config.find_config(None)
        self.assertIn("CALLTABLE_CONFIG", str(ctx.exception))

    def test_misplaced_config_flag_cannot_fall_back_to_a_real_config(self):
        # 复现曾经的事故形态：--config 写在 -- 之后，会被 argparse 的
        # REMAINDER 当成任务命令，CLI 于是没有显式配置。测试环境里
        # CALLTABLE_CONFIG 指向不存在的临时文件，因此必须立刻失败，
        # 而不是退回开发者真实的 ~/.config/calltable/config.json。
        repo = Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-m", "calltable", "cli", "run",
             "--project", "demo", "--", "echo", "--config",
             str(isolation.TEST_HOME / "somewhere.json")],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo), env=os.environ.copy())
        self.assertNotEqual(proc.returncode, 0)
        combined = (proc.stderr + proc.stdout).lower()
        self.assertIn("config", combined)
        self.assertNotIn(str(isolation.REAL_HOME).lower(),
                         combined.replace(str(isolation.TEST_HOME).lower(), ""))

    def test_subprocesses_inherit_the_test_home(self):
        script = ("import os, pathlib;"
                  "print(pathlib.Path.home());"
                  "print(os.environ.get('CALLTABLE_CONFIG', ''));"
                  "print(os.environ.get('USERPROFILE', ''))")
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=60,
                              env=os.environ.copy())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(Path(lines[0]).resolve(), isolation.TEST_HOME)
        self.assertEqual(Path(lines[1]), Path(os.environ["CALLTABLE_CONFIG"]))
        if os.name == "nt":
            self.assertEqual(Path(lines[2]).resolve(), isolation.TEST_HOME)


if __name__ == "__main__":
    unittest.main()
