"""仓库布局约束：可以直接执行的脚本必须在 git index 里带可执行位。

没带的话 clone 出来是 100644，`./bin/ct` 和手工软链都会报「权限不够」，
而开发者本地（已 chmod 过）完全看不出来。用 `git ls-files -s` 断言 index 里
的 mode，防止回退。
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 需要被直接执行（或软链后直接执行）的脚本。Windows 的 .ps1/.cmd 由文件
#: 关联决定怎么跑，不靠可执行位，故意不在此列。
EXECUTABLE_PATHS = (
    "bin/ct",
    "bin/install.sh",
    "calltable/cloudrun_assets/entrypoint.sh",
    "deploy/drain-restart.sh",
    "deploy/sync-remote.sh",
    "deploy/wsl/exec.sh",
)

#: 明确不需要可执行位的启动器。
NON_EXECUTABLE_PATHS = (
    "bin/ct.cmd",
    "bin/install.ps1",
    "deploy/windows/exec-restart.ps1",
    "deploy/windows/install-executor-task.ps1",
    "deploy/windows/install-wsl-executor-task.ps1",
)


def git_index_modes(root: Path) -> dict[str, str]:
    """path -> index mode（八进制字符串，如 "100755"）。"""
    out = subprocess.run(["git", "ls-files", "-s"], cwd=str(root),
                         capture_output=True, text=True, check=True)
    modes: dict[str, str] = {}
    for line in out.stdout.splitlines():
        meta, _, path = line.partition("\t")
        mode = meta.split()[0]
        modes[path] = mode
    return modes


class IndexModeTests(unittest.TestCase):
    def setUp(self):
        # 这两种情况都不是「可执行位丢了」：不在 git 工作区里（sdist、解压的
        # 源码包）读不到 index，读到了但为空说明不是本仓库的 index。
        if not (_REPO_ROOT / ".git").exists():
            self.skipTest(f"{_REPO_ROOT} 不是 git 工作区（无 .git），"
                          "读不到 git index，跳过可执行位检查")
        try:
            self.modes = git_index_modes(_REPO_ROOT)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"git ls-files 不可用（{exc}），跳过可执行位检查")
        if not self.modes:
            self.skipTest("git index 为空，跳过可执行位检查")

    def test_directly_executed_scripts_are_100755(self):
        wrong = {path: self.modes.get(path, "missing")
                 for path in EXECUTABLE_PATHS
                 if self.modes.get(path) != "100755"}
        self.assertEqual(
            wrong, {},
            "这些脚本在 git index 里不是 100755（clone 后不可执行），"
            f"用 `git update-index --chmod=+x <path>` 修：{wrong}")

    def test_windows_launchers_stay_100644(self):
        wrong = {path: self.modes.get(path, "missing")
                 for path in NON_EXECUTABLE_PATHS
                 if self.modes.get(path) != "100644"}
        self.assertEqual(wrong, {}, f"这些文件不该带可执行位：{wrong}")

    def test_every_tracked_shell_script_is_listed(self):
        # 新增 .sh 却忘了加进 EXECUTABLE_PATHS 时，这条会提示补进来。
        tracked = {path for path in self.modes if path.endswith(".sh")}
        self.assertEqual(
            tracked - set(EXECUTABLE_PATHS) - set(NON_EXECUTABLE_PATHS), set(),
            "有 .sh 没被可执行位测试覆盖，请加到 EXECUTABLE_PATHS")


if __name__ == "__main__":
    unittest.main()
