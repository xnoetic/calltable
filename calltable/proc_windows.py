"""Windows 进程控制：Job Object（进程树 + 资源采样）。

为什么用 Job Object
-------------------
* ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` 保证执行器自己崩掉时，任务进程树
  也会被内核回收，不留孤儿；
* ``QueryInformationJobObject`` 给出整棵树的累计 CPU 与峰值提交内存，正好是
  看板要的「占了多少资源」；
* 取消时 ``TerminateJobObject`` 一次结束全部后代，比 ``taskkill /T`` 更可靠
  （后者仍作为兜底，用于 Job 创建失败的降级路径）。

采样口径：CPU = TotalUserTime + TotalKernelTime（100ns → 微秒）；
mem_peak = JobObjectExtendedLimitInformation.PeakJobMemoryUsed；
mem_current = 作业内各进程 WorkingSetSize 之和（K32GetProcessMemoryInfo）。
拿不到时（权限/旧系统）当前值退回 0，峰值退回 job 的峰值；两者都拿不到时
由执行器退回 ``tasklist`` 仅做存活检查。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time

from .proc_types import Sample, TreeProcess

# 非 Windows 平台也要能 import 本模块（macOS 上的客户端不执行这里，但
# `import calltable.proc_windows` 不能直接炸）。
if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
else:  # pragma: no cover - 仅在非 Windows 上走到
    kernel32 = None
    psapi = None

JobObjectBasicAccountingInformation = 1
JobObjectBasicProcessIdList = 3
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15

JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_SUSPENDED = 0x00000004
NORMAL_PRIORITY_CLASS = 0x00000020
PROCESS_SUSPEND_RESUME = 0x0800

ULONG_PTR = ctypes.c_size_t
SIZE_T = ctypes.c_size_t
LARGE_INTEGER = ctypes.c_longlong


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", LARGE_INTEGER),
        ("TotalKernelTime", LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", LARGE_INTEGER),
        ("TotalPageFaultCount", wt.DWORD),
        ("TotalProcesses", wt.DWORD),
        ("ActiveProcesses", wt.DWORD),
        ("TotalTerminatedProcesses", wt.DWORD),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    # ControlFlags 后面是 union；HARD_CAP 用 CpuRate（1/100 百分比，10000=100%）。
    _fields_ = [
        ("ControlFlags", wt.DWORD),
        ("CpuRate", wt.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", SIZE_T),
        ("WorkingSetSize", SIZE_T),
        ("QuotaPeakPagedPoolUsage", SIZE_T),
        ("QuotaPagedPoolUsage", SIZE_T),
        ("QuotaPeakNonPagedPoolUsage", SIZE_T),
        ("QuotaNonPagedPoolUsage", SIZE_T),
        ("PagefileUsage", SIZE_T),
        ("PeakPagefileUsage", SIZE_T),
    ]


if kernel32 is None:  # pragma: no cover - 非 Windows 占位，让 import 成功
    def _missing(*args, **kwargs):
        raise OSError("Windows Job Object API is unavailable on this platform")

    class _Missing:
        def __getattr__(self, name):
            return _missing

    kernel32 = _Missing()  # type: ignore[assignment]
    psapi = _Missing()  # type: ignore[assignment]

kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.SetInformationJobObject.restype = wt.BOOL
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wt.DWORD]
kernel32.QueryInformationJobObject.restype = wt.BOOL
kernel32.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wt.DWORD,
                                               ctypes.POINTER(wt.DWORD)]
kernel32.AssignProcessToJobObject.restype = wt.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.TerminateJobObject.restype = wt.BOOL
kernel32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetExitCodeProcess.restype = wt.BOOL
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.OpenThread.restype = wt.HANDLE
kernel32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.ResumeThread.restype = wt.DWORD
kernel32.ResumeThread.argtypes = [wt.HANDLE]
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.Thread32First.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.Thread32Next.argtypes = [wt.HANDLE, ctypes.c_void_p]
try:
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.restype = ctypes.c_long
    ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
except OSError:  # pragma: no cover - 极老系统
    ntdll = None
def _resolve_memory_info():
    """K32GetProcessMemoryInfo 通常从 kernel32 转发；老系统只有 psapi 版本。"""
    for dll, name in ((kernel32, "K32GetProcessMemoryInfo"),
                      (psapi, "GetProcessMemoryInfo")):
        try:
            fn = getattr(dll, name)
        except AttributeError:
            continue
        fn.restype = wt.BOOL
        fn.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                       wt.DWORD]
        return fn
    return None


_GET_PROCESS_MEMORY_INFO = _resolve_memory_info()


class JobImpl:
    def __init__(self, handle):
        self.handle = handle
        self._closed = False

    # ---------------------------------------------------------- 采样
    def sample(self) -> Sample:
        if self._closed or not self.handle:
            return Sample()
        cpu_usec = 0
        accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if kernel32.QueryInformationJobObject(
                self.handle, JobObjectBasicAccountingInformation,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            cpu_usec = int((accounting.TotalUserTime + accounting.TotalKernelTime) / 10)

        peak = 0
        extended = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if kernel32.QueryInformationJobObject(
                self.handle, JobObjectExtendedLimitInformation,
                ctypes.byref(extended), ctypes.sizeof(extended), None):
            peak = int(extended.PeakJobMemoryUsed)

        current = 0
        for pid in self._pids():
            current += _working_set(pid)
        if not peak:
            peak = current
        return Sample(cpu_usec=cpu_usec, mem_current=current, mem_peak=peak)

    def _pids(self) -> list[int]:
        capacity = 4096
        size = 8 + capacity * ctypes.sizeof(ULONG_PTR)
        buffer = ctypes.create_string_buffer(size)
        if not kernel32.QueryInformationJobObject(
                self.handle, JobObjectBasicProcessIdList, buffer, size, None):
            return []
        count = ctypes.cast(buffer, ctypes.POINTER(wt.DWORD))[1]
        count = min(int(count), capacity)
        array_type = ULONG_PTR * max(count, 1)
        array = array_type.from_buffer(buffer, 8)
        return [int(array[i]) for i in range(count) if int(array[i])]

    # ---------------------------------------------------------- 终止
    def terminate(self, grace: float) -> None:
        if self._closed or not self.handle:
            return
        # 先给 3 秒的优雅窗口：TerminateJobObject 只能强杀，所以先发 CTRL_BREAK
        # 让测试脚本有机会自己收尾（尽力而为，失败不影响后面的强杀）。
        try:
            pids = self._pids()
            if pids:
                subprocess.run(["taskkill", "/T", "/PID", str(pids[0])],
                               capture_output=True, timeout=max(1, int(grace)))
            time.sleep(min(max(grace, 0.0), 2.0))
        except (OSError, subprocess.SubprocessError):
            pass
        kernel32.TerminateJobObject(self.handle, 1)
        # 兜底：taskkill /T /F 清掉 Job 之外的残渣
        for pid in self._pids():
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            kernel32.TerminateJobObject(self.handle, 1)
        except Exception:
            pass
        try:
            kernel32.CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None


def _working_set(pid: int) -> int:
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return 0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if _GET_PROCESS_MEMORY_INFO is None:
            return 0
        if not _GET_PROCESS_MEMORY_INFO(handle, ctypes.byref(counters),
                                        ctypes.sizeof(counters)):
            return 0
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def _apply_cpu_rate(handle, percent: int | None) -> bool:
    if not handle or not percent:
        return False
    info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
    info.ControlFlags = (JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                         | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP)
    info.CpuRate = max(1, min(100, int(percent))) * 100
    return bool(kernel32.SetInformationJobObject(
        handle, JobObjectCpuRateControlInformation,
        ctypes.byref(info), ctypes.sizeof(info)))


def spawn(*, argv: list[str], cwd, env, job_id: str,
          cpu_rate_percent: int | None = None) -> TreeProcess:
    """CREATE_SUSPENDED 启动 → 分配进 Job Object → 恢复主线程（I10）。

    这样子进程在被作业接管之前不会跑出任何一条指令，避免极短任务在
    ``AssignProcessToJobObject`` 之前就 fork 出逃逸的孙进程。
    Job 创建失败时仍然恢复进程，只是没有 Job 语义；terminate/close 会退回
    ``taskkill /T /F``。
    """
    handle = kernel32.CreateJobObjectW(None, None)
    attached = False
    if handle:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))

    popen = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # 正常优先级：任务已经受 Job Object 的硬 CPU 上限约束；再用
        # BELOW_NORMAL 会在别人跑构建时被长期饿死（实测 4h 墙钟）。
        creationflags=(CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED
                       | NORMAL_PRIORITY_CLASS),
    )
    proc = TreeProcess(popen, job_id)

    if handle:
        process_handle = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
            | PROCESS_SUSPEND_RESUME,
            False, popen.pid)
        if process_handle:
            attached = bool(kernel32.AssignProcessToJobObject(handle, process_handle))
            if attached:
                _apply_cpu_rate(handle, cpu_rate_percent)
            else:
                kernel32.CloseHandle(handle)
                handle = None
    else:
        process_handle = None

    # 恢复被 CREATE_SUSPENDED 挂起的主线程。
    if process_handle:
        try:
            if ntdll is not None:
                ntdll.NtResumeProcess(process_handle)
            else:  # pragma: no cover - 退路
                _resume_threads(popen.pid)
        finally:
            kernel32.CloseHandle(process_handle)
    else:  # pragma: no cover - 极端情况下退回按线程恢复
        _resume_threads(popen.pid)

    if attached:
        proc.attach_impl(JobImpl(handle))
    return proc


def _resume_threads(pid: int) -> None:  # pragma: no cover - 仅退路
    """枚举进程线程并 ResumeThread（NtResumeProcess 不可用时）。"""
    TH32CS_SNAPTHREAD = 0x00000004

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ThreadID", wt.DWORD), ("th32OwnerProcessID", wt.DWORD),
                    ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                    ("dwFlags", wt.DWORD)]

    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if thread:
                    kernel32.ResumeThread(thread)
                    kernel32.CloseHandle(thread)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                   capture_output=True, timeout=20)


def pid_alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    exit_code = wt.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
