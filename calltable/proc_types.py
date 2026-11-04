"""进程树抽象（避免 proc/proc_linux/proc_windows 之间的循环导入）。"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class Sample:
    cpu_usec: int = 0
    mem_current: int = 0
    mem_peak: int = 0
    tasks: int = 0


class TreeProcess:
    """一个任务进程（可能带 systemd scope 或 Windows Job Object）。"""

    def __init__(self, popen: subprocess.Popen, job_id: str):
        self.popen = popen
        self.job_id = job_id
        self._impl = None

    def attach_impl(self, impl) -> None:
        self._impl = impl

    def poll(self) -> int | None:
        return self.popen.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.popen.wait(timeout=timeout)

    def sample(self) -> Sample:
        if self._impl is None:
            return Sample()
        try:
            return self._impl.sample()
        except Exception:
            return Sample()

    @property
    def soft_limit_exceeded(self) -> bool:
        """direct 模式的软内存上限已被连续采样确认超限。"""
        return bool(getattr(self._impl, "exceeded", False))

    def terminate(self, grace: float = 5.0) -> None:
        """先温和终止，grace 秒后强杀整棵树。可重复调用。"""
        if self._impl is not None:
            try:
                self._impl.terminate(grace)
            except Exception:
                pass
        if self.popen.poll() is None:
            # 没有可用实现（或实现杀不动）时按平台兜底：整个进程树。
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID",
                                    str(self.popen.pid)],
                                   capture_output=True, timeout=20)
                else:
                    os.killpg(self.popen.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                self.popen.kill()
            except Exception:
                pass

    def close(self) -> None:
        if self._impl is not None:
            try:
                self._impl.close()
            except Exception:
                pass
        try:
            if self.popen.poll() is None:
                self.popen.kill()
                self.popen.wait(timeout=10)
        except Exception:
            pass
        try:
            if self.popen.stdout is not None:
                self.popen.stdout.close()
        except Exception:
            pass
