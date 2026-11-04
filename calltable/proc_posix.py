"""没有 ``/proc`` 的 POSIX（macOS / BSD）：用 ``ps`` 采样整棵进程树。

Linux 的 direct 模式靠 ``/proc`` 拿 pid/ppid/session/RSS/CPU tick；macOS 没有
``/proc``，所以同一套逻辑换成一次 ``ps -axo`` 输出。采样口径、软内存上限、
兜底 kill 都在 :mod:`calltable.proc_tree` 里共用。

进程组（pgid）在这里代替 session id：``spawn`` 用 ``start_new_session=True``
拉起任务，任务进程的 session 和 pgid 都等于它自己的 pid，后代默认继承同一个
pgid（非交互 shell 不开作业控制），所以「同一个 pgid + 父子闭包」就是整棵树。
"""

from __future__ import annotations

import os
import subprocess

from .proc_tree import TreeSampler
from .proc_types import TreeProcess

#: 列顺序与 `_proc_table` 的解析必须一致。
PS_COLUMNS = "pid=,ppid=,pgid=,rss=,time="
#: `ps` 在机器很忙时要几秒；超时就当作「这一轮没采到」。
PS_TIMEOUT = 15


def parse_cpu_seconds(text: str) -> float:
    """解析 ``ps time=`` 的累计 CPU 时间：``[[dd-]hh:]mm:ss[.cc]``。"""
    text = text.strip()
    if not text:
        return 0.0
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return 0.0
    try:
        numbers = [float(part) for part in text.split(":")]
    except ValueError:
        return 0.0
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours, minutes, seconds = 0.0, numbers[0], numbers[1]
    elif len(numbers) == 1:
        hours, minutes, seconds = 0.0, 0.0, numbers[0]
    else:
        return 0.0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_ps_output(text: str, clk_tck: int) -> dict[int, dict]:
    """``ps -axo pid=,ppid=,pgid=,rss=,time=`` → 进程表（字段同 /proc 口径）。

    RSS 用 ``rss`` 列（KB）；CPU 时间统一折算成 tick，复用 /proc 路径的
    ``utime + stime`` 累加方式。
    """
    table: dict[int, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        try:
            pid, ppid, pgid, rss_kb = (int(parts[0]), int(parts[1]),
                                       int(parts[2]), int(parts[3]))
        except ValueError:
            continue
        ticks = int(round(parse_cpu_seconds(parts[4]) * clk_tck))
        table[pid] = {
            "state": "?",
            "ppid": ppid,
            "session": pgid,
            "utime": max(0, ticks),
            "stime": 0,
            "rss": max(0, rss_kb) * 1024,
        }
    return table


class PsImpl(TreeSampler):
    """`ps` 版 direct 采样器：接口与 ``proc_linux.DirectImpl`` 一致。"""

    def _proc_table(self) -> dict[int, dict]:
        try:
            completed = subprocess.run(
                ["ps", "-axo", PS_COLUMNS], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=PS_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return {}
        return parse_ps_output(completed.stdout or "", self.CLK_TCK)

    def _rss_of(self, table: dict[int, dict], pid: int) -> int:
        # 一次 `ps` 已经带了 RSS，不用再为每个 pid 起一次进程。
        return int((table.get(pid) or {}).get("rss", 0))


def spawn_direct(*, argv: list[str], cwd, env, job_id: str,
                 soft_limit_bytes: int | None = None) -> TreeProcess:
    popen = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    proc = TreeProcess(popen, job_id)
    proc.attach_impl(PsImpl(popen.pid, soft_limit_bytes=soft_limit_bytes))
    return proc


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(pid: int) -> None:
    from .proc_tree import SIGKILL
    try:
        os.killpg(pid, SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
