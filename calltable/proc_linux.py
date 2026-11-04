"""Linux 进程控制：systemd-run --user --scope 或 setsid 进程组。

实测结论（systemd 257 / Debian 13）
------------------------------------------------
* ``systemd-run --user --scope --unit X --collect --quiet -- ...`` 退出码正确透传；
* ``systemctl --user kill --signal=SIGKILL X.scope`` 能杀掉整棵进程树；
* **不能读 cgroup 文件**：在某些容器化部署里（例如 runc 容器），
  ``ControlGroup`` 返回的是从宿主根开始的长路径，容器内 ``/sys/fs/cgroup`` 下
  并不存在。因此采样一律走 ``systemctl --user show`` 的四个属性：
  ``MemoryCurrent`` / ``MemoryPeak`` / ``CPUUsageNSec`` / ``TasksCurrent``。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time

from .proc_types import Sample, TreeProcess

UNIT_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
PROBE_PREFIX = "calltable-probe-"
#: Windows 没有 SIGKILL；模块级常量让代码/测试都能在任意平台导入与打桩。
SIGTERM = getattr(signal, "SIGTERM", 15)
SIGKILL = getattr(signal, "SIGKILL", 9)


def unit_name(job_id: str) -> str:
    return "calltable-" + UNIT_SAFE.sub("-", job_id)[:80]


def parse_systemd_show(text: str) -> Sample:
    """解析 ``systemctl --user show -p ...`` 的 key=value 输出。"""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, raw = line.partition("=")
        if not sep:
            continue
        raw = raw.strip()
        if not raw or raw in ("infinity", "[not set]", "n/a"):
            continue
        try:
            number = int(raw)
        except ValueError:
            continue
        if number < 0:
            continue
        values[key] = number
    return Sample(
        cpu_usec=values.get("CPUUsageNSec", 0) // 1000,
        mem_current=values.get("MemoryCurrent", 0),
        mem_peak=values.get("MemoryPeak", 0) or values.get("MemoryCurrent", 0),
        tasks=values.get("TasksCurrent", 0),
    )


def _systemd_run(argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def probe() -> tuple[bool, str]:
    """真正跑一次 systemd-run 探测（B2）。返回 (可用, 原因)。"""
    if not shutil.which("systemd-run"):
        return False, "systemd-run not found"
    unit = PROBE_PREFIX + str(os.getpid())
    full_unit = unit + ".scope"
    try:
        proc = _systemd_run([
            "systemd-run", "--user", "--scope", "--unit", unit, "--collect",
            "--quiet", "--", "true",
        ])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            _systemd_run(["systemctl", "--user", "reset-failed", full_unit])
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.returncode == 0:
        return True, "ok"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, (detail[-1] if detail else f"rc={proc.returncode}")


class SystemdImpl:
    def __init__(self, unit: str, pid: int):
        # systemd-run --unit X 实际创建的是 X.scope；systemctl 不会自动补
        # .scope 后缀（缺省按 .service 找），所以这里保存完整 unit 名。
        self.unit = unit if unit.endswith(".scope") else unit + ".scope"
        self.pid = pid

    def sample(self) -> Sample:
        try:
            proc = _systemd_run(
                ["systemctl", "--user", "show",
                 "-p", "MemoryCurrent", "-p", "MemoryPeak",
                 "-p", "CPUUsageNSec", "-p", "TasksCurrent", self.unit],
                timeout=10)
        except (OSError, subprocess.SubprocessError):
            return Sample()
        if proc.returncode != 0:
            return Sample()
        return parse_systemd_show(proc.stdout)

    def terminate(self, grace: float) -> None:
        ok = False
        try:
            result = _systemd_run(
                ["systemctl", "--user", "kill", "--signal", "SIGTERM", self.unit],
                timeout=10)
            ok = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            deadline = time.time() + max(grace, 0.5)
            while time.time() < deadline and _alive(self.pid):
                time.sleep(0.1)
        # 无论 systemctl 成不成功，都兜底打进程组（spawn 时 setsid）。
        try:
            os.killpg(self.pid, SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            _systemd_run(["systemctl", "--user", "kill", "--signal", "SIGKILL",
                          self.unit], timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def close(self) -> None:
        # 任务正常结束后 scope 里可能还留着后台进程（例如原生 PG）。
        # 无条件 kill 一次再 reset-failed，避免 scope 与子进程泄漏。
        try:
            _systemd_run(["systemctl", "--user", "kill", "--signal", "SIGKILL",
                          self.unit], timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            _systemd_run(["systemctl", "--user", "reset-failed", self.unit],
                         timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


class DirectImpl:
    """没有 systemd 时的退路：setsid 进程组 + /proc 整棵进程树采样。

    采样口径：按 /proc 里的 **session id**（setsid 后整棵树同一个 session）
    加上父子关系闭包找出全部后代，累加 RSS 与 utime+stime；峰值取历史最大值
    （并至少不低主进程的 VmHWM）。连续两次采样都超过 ``soft_limit_bytes``
    时置 ``exceeded``，由执行器杀掉整棵树并记为 error。
    """

    CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    #: 连续多少次采样超过软上限才动手（避免构建尖峰误杀）。
    MEMORY_LIMIT_STRIKES = 2

    def __init__(self, pid: int, soft_limit_bytes: int | None = None):
        self.pid = pid
        self.soft_limit_bytes = int(soft_limit_bytes or 0)
        self.exceeded = False
        self._over_strikes = 0
        self._peak = 0
        self._last_cpu_usec = 0

    # ---------------------------------------------------------- /proc 扫描
    @staticmethod
    def _proc_table() -> dict[int, dict]:
        table: dict[int, dict] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return table
        for name in entries:
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii",
                          errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            _, _, rest = text.rpartition(")")
            fields = rest.split()
            if len(fields) < 22:
                continue
            try:
                table[pid] = {
                    "state": fields[0],
                    "ppid": int(fields[1]),
                    "session": int(fields[3]),
                    "utime": int(fields[11]),
                    "stime": int(fields[12]),
                }
            except (IndexError, ValueError):
                continue
        return table

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

    @staticmethod
    def _rss_bytes(pid: int) -> int:
        try:
            with open(f"/proc/{pid}/status", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return 0

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
        current = sum(self._rss_bytes(pid) for pid in pids)
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
                if not _alive(self.pid):
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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_alive(pid: int) -> bool:
    return _alive(pid)


def kill_tree(pid: int) -> None:
    try:
        os.killpg(pid, SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def spawn_systemd(*, argv: list[str], cwd, env, job_id: str,
                  memory_max: str | None, cpu_weight: str | None,
                  slice: str | None = None) -> TreeProcess:
    unit = unit_name(job_id)
    cmd = ["systemd-run", "--user", "--scope", "--unit", unit, "--collect",
           "--quiet"]
    if slice:
        cmd += ["--slice", slice]
    if memory_max:
        cmd += ["-p", f"MemoryMax={memory_max}"]
    if cpu_weight:
        cmd += ["-p", f"CPUWeight={cpu_weight}"]
    cmd += ["--", *argv]
    popen = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    proc = TreeProcess(popen, job_id)
    proc.attach_impl(SystemdImpl(unit + ".scope", popen.pid))
    return proc


def spawn_direct(*, argv: list[str], cwd, env, job_id: str,
                 soft_limit_bytes: int | None = None) -> TreeProcess:
    popen = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    proc = TreeProcess(popen, job_id)
    proc.attach_impl(DirectImpl(popen.pid, soft_limit_bytes=soft_limit_bytes))
    return proc
