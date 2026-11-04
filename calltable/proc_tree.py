"""进程树采样的公共部分：哪些 pid 属于这棵树、CPU/内存怎么累加。

具体「进程表从哪来」由子类提供：

* Linux 有 ``/proc``（``calltable.proc_linux.DirectImpl``）；
* macOS 没有 ``/proc``，用 ``ps``（``calltable.proc_posix.PsImpl``）。

采样口径：按 session id（``setsid`` 后整棵树同一个 session）加上父子关系闭包
找出全部后代，累加 RSS 与 utime+stime；峰值取历史最大值。连续两次采样都超过
``soft_limit_bytes`` 时置 ``exceeded``，由执行器杀掉整棵树并记为 error。

进程表条目的字段固定为 ``state`` / ``ppid`` / ``session`` / ``utime`` /
``stime``（utime+stime 用 tick 表示）。
"""

from __future__ import annotations

import os
import signal
import time

from .proc_types import Sample

SIGTERM = getattr(signal, "SIGTERM", 15)
SIGKILL = getattr(signal, "SIGKILL", 9)


def clock_ticks() -> int:
    """``sysconf(_SC_CLK_TCK)``；拿不到时退回 100（Linux/macOS 的常见值）。"""
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100


class TreeSampler:
    """整棵进程树的 CPU / RSS 采样 + 软内存上限 + 兜底 kill。"""

    CLK_TCK = clock_ticks()
    #: 连续多少次采样超过软上限才动手（避免构建尖峰误杀）。
    MEMORY_LIMIT_STRIKES = 2

    def __init__(self, pid: int, soft_limit_bytes: int | None = None):
        self.pid = pid
        self.soft_limit_bytes = int(soft_limit_bytes or 0)
        self.exceeded = False
        self._over_strikes = 0
        self._peak = 0
        self._last_cpu_usec = 0

    # ---------------------------------------------------------- 进程表
    def _proc_table(self) -> dict[int, dict]:
        """当前进程表：``{pid: {...}}``；拿不到时返回空 dict。"""
        raise NotImplementedError

    def _rss_bytes(self, pid: int) -> int:
        """单个进程的常驻内存（字节）。"""
        return 0

    def _rss_of(self, table: dict[int, dict], pid: int) -> int:
        """某个进程的 RSS。默认逐 pid 去读（/proc）；ps 版直接查表。"""
        return self._rss_bytes(pid)

    def _pids(self, table: dict[int, dict] | None = None) -> list[int]:
        table = table if table is not None else self._proc_table()
        selected = {pid for pid, entry in table.items()
                    if entry["session"] == self.pid}
        selected.add(self.pid)
        children: dict[int, list[int]] = {}
        for pid, entry in table.items():
            children.setdefault(entry["ppid"], []).append(pid)
        # 父子闭包：兜住自己 setsid 出去的子进程。
        frontier = list(selected)
        while frontier:
            pid = frontier.pop()
            for child in children.get(pid, ()):
                if child not in selected:
                    selected.add(child)
                    frontier.append(child)
        return [pid for pid in selected if pid in table]

    # ---------------------------------------------------------- 采样
    def sample(self) -> Sample:
        table = self._proc_table()
        if not table:
            return Sample()
        pids = self._pids(table)
        ticks = 0
        for pid in pids:
            entry = table.get(pid)
            if entry:
                ticks += entry["utime"] + entry["stime"]
        cpu_usec = int(ticks * 1_000_000 / self.CLK_TCK)
        if cpu_usec < self._last_cpu_usec:
            cpu_usec = self._last_cpu_usec  # 子进程被回收时计数器不能倒退
        self._last_cpu_usec = cpu_usec
        current = sum(self._rss_of(table, pid) for pid in pids)
        self._peak = max(self._peak, current)
        if self.soft_limit_bytes:
            if current > self.soft_limit_bytes:
                self._over_strikes += 1
                if self._over_strikes >= self.MEMORY_LIMIT_STRIKES:
                    self.exceeded = True
            else:
                self._over_strikes = 0
        return Sample(cpu_usec=cpu_usec, mem_current=current,
                      mem_peak=max(self._peak, current),
                      tasks=len(pids))

    # ---------------------------------------------------------- 终止
    def terminate(self, grace: float) -> None:
        try:
            pids = self._pids()
        except Exception:  # pragma: no cover - 兜底
            pids = []
        for sig in (SIGTERM, SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                break
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.time() + grace
            while time.time() < deadline:
                if not alive(self.pid):
                    return
                time.sleep(0.1)

    def close(self) -> None:
        try:
            os.killpg(self.pid, SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for pid in self._pids():
            try:
                os.kill(pid, SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
