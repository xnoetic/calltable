"""执行器磁盘水位保护 + 主机资源快照。

规则（每台执行主机各一份配置）：

* ``min_free_gb``：runs_root 所在分区的**空闲**下限；
* ``runs_max_gb``：runs_root 目录**总大小**上限。

每次 claim 前检查；超水位时先按完成时间从旧到新删已结束的 run 目录
（可以早于保留期，但**绝不**碰正在跑的），清完仍不够就不再 claim，
并在心跳里上报 ``disk_low``；hub 会把 disk_low 的主机从调度里跳过。

本模块只做纯计算 + 明确的删除动作，方便单测注入假的 free/size 函数。
"""

from __future__ import annotations

import os
import shutil
import stat as stat_module
import time
from dataclasses import dataclass, field
from pathlib import Path

GB = 1024 ** 3

#: 已完成目录里用来标记完成时间的文件名（没有时退回目录 mtime）。
FINISHED_MARKER = ".finished"


#: 单次 runs 大小扫描的时间预算（秒）。runs 里可能是 node_modules 森林，
#: 全量 walk 在机械盘上要几分钟，心跳/claim 不能被它拖死。
DEFAULT_SCAN_BUDGET = 8.0


@dataclass
class DiskStatus:
    free: int = 0
    runs: int = 0
    runs_partial: bool = False
    min_free_gb: float = 0.0
    runs_max_gb: float = 0.0
    low: bool = False
    reason: str = ""
    reclaimed: int = 0
    reclaimed_bytes: int = 0
    #: 本轮想删却删不干净的 run 目录名（例如容器以 root 写进来的文件）。
    undeletable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "disk_free_gb": round(self.free / GB, 1),
            "disk_runs_gb": round(self.runs / GB, 1),
            "min_free_gb": self.min_free_gb,
            "runs_max_gb": self.runs_max_gb,
            "disk_low": self.low,
            "disk_reason": self.reason,
            "disk_runs_partial": self.runs_partial,
            "disk_undeletable": len(self.undeletable),
        }


def directory_size(path: Path, deadline: float | None = None) -> int:
    """目录总大小；符号链接不跟随，错误静默跳过。超过 deadline 提前返回。"""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        if deadline is not None and time.monotonic() >= deadline:
            break
        dirnames[:] = [name for name in dirnames
                       if not (Path(dirpath) / name).is_symlink()]
        for name in filenames:
            try:
                entry = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat_module.S_ISREG(entry.st_mode):
                total += entry.st_size
    return total


def finished_at(run_dir: Path) -> float:
    """完成时间：优先读 ``.finished``，否则用目录 mtime。"""
    try:
        return float((run_dir / FINISHED_MARKER).read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        return run_dir.stat().st_mtime
    except OSError:
        return 0.0


def cleanup_candidates(runs_root: Path, protected: set[str]) -> list[Path]:
    """可被提前删掉的 run 目录：不是本次在执行的任务、也没有 ``.running``。"""
    candidates: list[Path] = []
    try:
        entries = list(runs_root.iterdir())
    except OSError:
        return candidates
    for entry in entries:
        if not entry.is_dir() or entry.name in protected:
            continue
        if (entry / ".running").exists():
            continue
        candidates.append(entry)
    candidates.sort(key=finished_at)
    return candidates


def runs_size(runs_root: Path, cache: dict | None = None,
              budget: float = DEFAULT_SCAN_BUDGET) -> tuple[int, bool]:
    """runs 目录总大小 + 是否因时间预算不足而只统计了一部分。

    ``cache`` 把「目录名 → (mtime, 字节数)」记下来：已完成目录不再变化，
    下次扫描直接复用，只有新目录/正在写的目录需要重新 walk。
    """
    deadline = time.monotonic() + max(0.0, float(budget or 0))
    total = 0
    partial = False
    seen: set[str] = set()
    try:
        entries = list(runs_root.iterdir())
    except OSError:
        return 0, False
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        seen.add(entry.name)
        cached = (cache or {}).get(entry.name)
        if cached and cached[0] == mtime:
            total += cached[1]
            continue
        if time.monotonic() >= deadline:
            partial = True
            if cached:
                total += cached[1]
            continue
        size = directory_size(entry, deadline=deadline)
        total += size
        if time.monotonic() >= deadline:
            # walk 可能被截断：不要把这个偏小的值当成完整大小缓存下来。
            partial = True
            continue
        if cache is not None:
            cache[entry.name] = (mtime, size)
    if cache is not None:
        for name in list(cache):
            if name not in seen:
                # 心跳线程和清理线程共用这份缓存，别的线程可能刚删过这一项。
                cache.pop(name, None)
    return total, partial


def _over_reason(status: DiskStatus) -> str:
    if status.min_free_gb and status.free < status.min_free_gb * GB:
        return (f"free {status.free / GB:.1f}GB < "
                f"{status.min_free_gb:.0f}GB")
    # 扫描被预算截断时 runs 只是下限，不足以触发 runs_max_gb，
    # 但 min_free_gb 仍然盯着分区空闲。
    if (status.runs_max_gb and not status.runs_partial
            and status.runs > status.runs_max_gb * GB):
        return (f"runs {status.runs / GB:.1f}GB > "
                f"{status.runs_max_gb:.0f}GB")
    return ""


def measure(runs_root: Path, *, min_free_gb: float = 0,
            runs_max_gb: float = 0, free_fn=None, cache: dict | None = None,
            scan_budget: float = DEFAULT_SCAN_BUDGET) -> DiskStatus:
    """只测量、不删除（心跳/claim 前检查用，永远不会阻塞在 rmtree 上）。"""
    free_fn = free_fn or (lambda path: shutil.disk_usage(path).free)
    status = DiskStatus(min_free_gb=float(min_free_gb or 0),
                        runs_max_gb=float(runs_max_gb or 0))
    try:
        status.free = int(free_fn(runs_root))
    except OSError:
        status.free = 0
    status.runs, status.runs_partial = runs_size(runs_root, cache, scan_budget)
    reason = _over_reason(status)
    status.low = bool(reason)
    status.reason = reason
    return status


def _known_size(run_dir: Path, cache: dict | None) -> int:
    """删除前的目录大小：优先用扫描缓存里的完整值。

    缓存没有（或目录又变过）才限时 walk；限时 walk 对大目录会偏小，
    拿它去扣 runs 总量会让清理多删好几个目录。
    """
    cached = (cache or {}).get(run_dir.name)
    if cached:
        try:
            if cached[0] == run_dir.stat().st_mtime:
                return cached[1]
        except OSError:
            pass
    return directory_size(run_dir, deadline=time.monotonic() + 2)


def enforce(runs_root: Path, *, min_free_gb: float, runs_max_gb: float,
            protected: set[str] | None = None, free_fn=None,
            max_removals: int = 400, cache: dict | None = None,
            scan_budget: float = DEFAULT_SCAN_BUDGET) -> DiskStatus:
    """检查水位；不够就删最旧的已结束目录。返回最终状态。

    删除可能很慢（几十 GB 的 run 目录），调用方要放在后台线程/janitor 里。
    删不干净的目录（典型是容器以 root 写进来、执行器用户删不动的文件）本轮
    不再重试，记进 ``status.undeletable``，接着删下一个；否则它会一直是
    「最旧的候选」，循环永远退不出来。
    """
    protected = protected or set()
    free_fn = free_fn or (lambda path: shutil.disk_usage(path).free)
    status = measure(runs_root, min_free_gb=min_free_gb,
                     runs_max_gb=runs_max_gb, free_fn=free_fn,
                     cache=cache, scan_budget=scan_budget)
    reason = status.reason
    stuck: set[str] = set()
    while reason and status.reclaimed < max_removals:
        candidates = [entry for entry in cleanup_candidates(runs_root, protected)
                      if entry.name not in stuck]
        if not candidates:
            break
        victim = candidates[0]
        size = _known_size(victim, cache)
        shutil.rmtree(victim, ignore_errors=True)
        if cache is not None:
            cache.pop(victim.name, None)
        if victim.exists():
            stuck.add(victim.name)
            status.undeletable.append(victim.name)
            # 删掉的那部分大小不清楚，只刷新空闲，runs 保守地不减。
            try:
                status.free = int(free_fn(runs_root))
            except OSError:
                pass
        else:
            status.reclaimed += 1
            status.reclaimed_bytes += size
            status.runs = max(0, status.runs - size)
            try:
                status.free = int(free_fn(runs_root))
            except OSError:
                status.free += size
        reason = _over_reason(status)

    status.low = bool(reason)
    status.reason = reason
    return status


# ------------------------------------------------------------------ 主机资源

def _linux_memory() -> tuple[int, int]:
    total = available = 0
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                value = rest.strip().split(" ")[0]
                if not value.isdigit():
                    continue
                if key == "MemTotal":
                    total = int(value) * 1024
                elif key == "MemAvailable":
                    available = int(value) * 1024
    except OSError:
        return 0, 0
    return total, available


def _windows_memory() -> tuple[int, int]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0, 0
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except Exception:  # pragma: no cover - 非 Windows / 老系统
        return 0, 0


def memory_bytes() -> tuple[int, int]:
    """(总内存, 可用内存)。拿不到时返回 (0, 0)。"""
    if os.name == "nt":
        return _windows_memory()
    return _linux_memory()


def load_average() -> float:
    try:
        return round(float(os.getloadavg()[0]), 2)
    except (OSError, AttributeError):
        return 0.0


def host_stats(runs_root: Path, *, min_free_gb: float = 0,
               runs_max_gb: float = 0, protected: set[str] | None = None,
               enforce_watermarks: bool = True) -> dict:
    """一次性算出心跳要上报的主机资源（含磁盘水位）。"""
    if enforce_watermarks:
        disk = enforce(runs_root, min_free_gb=min_free_gb,
                       runs_max_gb=runs_max_gb, protected=protected)
    else:
        disk = DiskStatus(min_free_gb=float(min_free_gb or 0),
                          runs_max_gb=float(runs_max_gb or 0))
        try:
            disk.free = int(shutil.disk_usage(runs_root).free)
        except OSError:
            disk.free = 0
        disk.runs = directory_size(runs_root)
    total, available = memory_bytes()
    payload = disk.to_dict()
    payload.update({
        "mem_total_gb": round(total / GB, 1),
        "mem_free_gb": round(available / GB, 1),
        "load1": load_average(),
    })
    return payload
