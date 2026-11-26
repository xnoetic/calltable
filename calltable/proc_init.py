"""执行器是 PID 1 时的最小 init：回收孤儿进程、转发信号。

背景
----
Cloud Run 镜像的 entrypoint 最后 ``exec python3 -m calltable exec ...``，容器
里没有 init，执行器自己就是 PID 1。任务留下的孤儿进程（父进程先退出的后台
进程）会被内核过继给 PID 1；它们退出、或被执行器按进程组杀掉之后没人
``wait``，就一直以僵尸形态占着 PID，直到整个 execution 结束。普通主机上
这些孤儿由系统的 init / systemd 回收，所以只有容器里才看得到。

为什么要多一个进程
------------------
不能在执行器进程里直接 ``waitpid(-1)``：那会把 ``subprocess`` 自己的子进程
（git、任务主进程）的退出状态抢走，``Popen`` 拿到 ECHILD 后会把返回码当成
0，失败的任务就被记成成功。「哪些子进程是过继来的」在内核里也查不出来。

所以做法和 tini / dumb-init 一样：PID 1 ``fork`` 一次，子进程回去继续当
执行器，PID 1 自己只做三件事：

* 把收到的信号原样转给执行器；
* ``waitpid(-1)`` 回收所有子进程（过继来的孤儿 + 执行器本身）；
* 执行器退出后用同样的退出码退出。PID 1 一退出，内核会杀掉这个 PID
  namespace 里剩下的所有进程，容器随之结束。

只在 POSIX 且 ``os.getpid() == 1`` 时生效。普通主机（systemd scope / 进程组 /
Windows Job Object）上执行器不是 PID 1，这里什么都不做。只用标准库。
"""

from __future__ import annotations

import os
import signal
import sys

#: 转给执行器的信号。PID 1 对「没装处理函数」的信号是免疫的，所以这里必须
#: 显式接住再转发；SIGKILL / SIGSTOP 接不住也不需要（PID 1 一死全容器都死）。
FORWARDED_NAMES = ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2")


def forwarded_signals() -> list[int]:
    return [getattr(signal, name) for name in FORWARDED_NAMES
            if hasattr(signal, name)]


def is_pid1() -> bool:
    return os.name == "posix" and os.getpid() == 1


def exit_code_of(status: int) -> int:
    """``waitpid`` 的状态 → 进程退出码；被信号杀死时按 shell 惯例取 128+N。"""
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def reap_until(child: int) -> int:
    """阻塞回收所有子进程，直到 ``child`` 退出；返回它的退出码。

    别的子进程（过继来的孤儿）回收后直接丢弃状态。信号处理函数在
    ``waitpid`` 被打断时运行，之后 Python 会自动重试（PEP 475）。
    """
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            # 一个子进程都没有了却没等到 child：只可能是有人替我们回收了它。
            return 1
        if pid == child:
            return exit_code_of(status)


def split() -> None:
    """``fork`` 出执行器：子进程从这里返回，父进程留下当 init，永不返回。

    调用方必须还是单线程（执行器的线程都在这之后才启动）。``fork`` 失败时
    打一条警告后原样返回，执行器照旧运行，只是孤儿不会被回收。
    """
    signals = forwarded_signals()
    # 缓冲区里的输出不能被父子进程各写一遍。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass
    # fork 前后信号先挡住：父进程装好转发函数之后才放开，中间到达的信号
    # 不会丢，也不会在父进程里触发默认的 KeyboardInterrupt。
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        child = os.fork()
    except OSError as exc:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        print(f"[exec] WARNING: running as PID 1 and fork failed ({exc}); "
              "orphaned job processes will not be reaped", file=sys.stderr,
              flush=True)
        return
    if child == 0:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        try:
            # 自己一个进程组：终端 Ctrl-C 是发给前台进程组的，不分组的话
            # 执行器会收到两次 SIGINT（一次来自终端，一次来自 PID 1 的转发）。
            os.setpgid(0, 0)
        except OSError:
            pass
        return

    code = 1
    try:
        def forward(signum, frame):
            try:
                os.kill(child, signum)
            except ProcessLookupError:
                pass

        for signum in signals:
            signal.signal(signum, forward)
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        code = reap_until(child)
    except BaseException as exc:  # pragma: no cover - PID 1 不能带着异常栈退出
        print(f"[exec] init: {type(exc).__name__}: {exc}", file=sys.stderr,
              flush=True)
    finally:
        # 不走解释器的正常退出：调用栈上层（cli.main）的收尾只该在执行器
        # 那个进程里跑一遍。
        os._exit(code)


def split_if_pid1() -> None:
    """执行器入口调用：是 PID 1 才拆出 init，否则什么都不做。"""
    if not is_pid1():
        return
    split()
    if not is_pid1():
        # 只有 fork 成功后的子进程走到这里。留一行日志，方便在容器日志里
        # 确认回收孤儿的 init 已经就位。
        print(f"[exec] started as PID 1 without an init: PID 1 stays behind "
              f"to reap orphaned processes, the executor continues as pid "
              f"{os.getpid()}", flush=True)
