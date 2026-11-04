"""子进程管理：统一的进程树接口 + 取消时杀干净整个进程树。

Linux：可选 ``systemd-run --user --scope``（cgroup v2 采样 + MemoryMax/CPUWeight），
        不可用时回退到 ``setsid`` 进程组，采样走 /proc。
Windows：Job Object（``KILL_ON_JOB_CLOSE`` + 进程树终止），采样走
        ``QueryInformationJobObject``；拿不到时回退 ``tasklist``。
"""

from __future__ import annotations

import os
from pathlib import Path

from .proc_types import Sample, TreeProcess  # noqa: F401  (对外统一从这里取)

# 平台实现按需导入：macOS/Windows 上 import calltable.proc 不能碰另一个平台的
# ctypes/WinDLL，否则客户端在 macOS 上连 CLI 都起不来。

__all__ = ["Sample", "TreeProcess", "enabled_systemd", "spawn",
           "kill_pid_tree", "pid_alive"]


def _linux():
    from . import proc_linux
    return proc_linux


def _windows():
    from . import proc_windows
    return proc_windows


def _direct():
    """direct 模式的实现：有 /proc 就用它，macOS/BSD 退回 ps。"""
    if os.path.isdir("/proc"):
        return _linux()
    from . import proc_posix
    return proc_posix


def probe_systemd() -> tuple[bool, str]:
    """真跑一次 systemd-run 探测；返回 (可用, 原因)。"""
    if os.name == "nt":
        return False, "windows"
    return _linux().probe()


def spawn(*, argv: list[str], cwd: str | Path, env: dict[str, str],
          job_id: str, use_systemd: bool = False,
          memory_max: str | None = None, cpu_weight: str | None = None,
          slice: str | None = None,
          cpu_rate_percent: int | None = None,
          soft_memory_bytes: int | None = None) -> TreeProcess:
    """启动任务进程。``argv`` 已经是可以直接 exec 的命令行。

    ``soft_memory_bytes`` 只在没有 systemd 的 Linux 降级路径上生效：进程树
    RSS 连续两次采样超过它就杀掉整棵树。
    """
    if os.name == "nt":
        return _windows().spawn(argv=argv, cwd=cwd, env=env, job_id=job_id,
                                cpu_rate_percent=cpu_rate_percent)
    if use_systemd:
        return _linux().spawn_systemd(
            argv=argv, cwd=cwd, env=env, job_id=job_id,
            memory_max=memory_max, cpu_weight=cpu_weight, slice=slice)
    return _direct().spawn_direct(argv=argv, cwd=cwd, env=env, job_id=job_id,
                                  soft_limit_bytes=soft_memory_bytes)


def kill_pid_tree(pid: int) -> None:
    if os.name == "nt":
        _windows().kill_tree(pid)
    else:
        _direct().kill_tree(pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows().pid_alive(pid)
    return _direct().pid_alive(pid)
