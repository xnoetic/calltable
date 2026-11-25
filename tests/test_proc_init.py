"""执行器当 PID 1 时的最小 init（``calltable.proc_init``）。

``waitpid(-1)`` 会回收调用进程的**所有**子进程，所以凡是真的 fork 的用例都放
在一次性的 Python 子进程里跑，不能在测试进程里直接调。

「孤儿被过继给谁」分两层验证：

* ``PR_SET_CHILD_SUBREAPER``：普通 Linux 主机上不是 PID 1 也能收养孤儿，足以
  验证回收逻辑本身；同一个脚本不启用 init 时孤儿会一直是僵尸（对照组，
  也就是修复前 Cloud Run 容器里的样子）。
* user + PID namespace（``unshare``）：脚本真的是 PID 1，验证入口
  ``split_if_pid1`` 的触发条件。主机不允许非特权 namespace 时跳过。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from calltable import executor as executor_module
from calltable import proc_init
from tests.helpers import UNSHARE_PID1, can_unshare_pid1

REPO_ROOT = Path(__file__).resolve().parents[1]
POSIX = os.name == "posix"
LINUX = sys.platform.startswith("linux")

#: 公共片段：造一个孤儿（父 shell 立刻退出），杀掉它，再看它有没有被回收。
#: 输出 ``reaper=<收养它的 pid>`` 和 ``orphan=reaped|zombie|running``。
ORPHAN_CHECK = r'''
import os, signal, subprocess, sys, time

def stat(pid):
    """(state, ppid)；进程不存在（已被回收）时返回 None。"""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            fields = handle.read().rpartition(")")[2].split()
    except OSError:
        return None
    return fields[0], int(fields[1])

def orphan_check(wait=10.0):
    out = subprocess.run(["sh", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
                         capture_output=True, text=True, check=True)
    orphan = int(out.stdout)
    info = stat(orphan)
    print(f"reaper={info[1] if info else None}", flush=True)
    os.kill(orphan, signal.SIGKILL)
    deadline = time.time() + wait
    while time.time() < deadline and stat(orphan) is not None:
        time.sleep(0.05)
    info = stat(orphan)
    state = "reaped" if info is None else ("zombie" if info[0] == "Z" else "running")
    print(f"orphan={state}", flush=True)
'''

SUBREAPER_SCRIPT = ORPHAN_CHECK + r'''
import ctypes
from calltable import proc_init

PR_SET_CHILD_SUBREAPER = 36
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    sys.exit(77)
adopter = os.getpid()
if sys.argv[1] == "init":
    proc_init.split()
print(f"adopter={adopter} self={os.getpid()}", flush=True)
# 对照组里没人会来回收，等再久也是僵尸，不用等满。
orphan_check(10.0 if sys.argv[1] == "init" else 1.5)
sys.exit(5)
'''

PID1_SCRIPT = ORPHAN_CHECK + r'''
from calltable import proc_init

before = os.getpid()
proc_init.split_if_pid1()
print(f"before={before} self={os.getpid()} parent={os.getppid()}", flush=True)
orphan_check()
sys.exit(5)
'''


def run_script(script: str, *args: str, prefix: list[str] | None = None,
               timeout: float = 60) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.run([*(prefix or []), sys.executable, "-c", script, *args],
                          cwd=str(REPO_ROOT), env=env, capture_output=True,
                          text=True, timeout=timeout)


class DecisionTests(unittest.TestCase):
    """什么时候拆 init：只看「是不是 POSIX 上的 PID 1」。"""

    def test_nothing_happens_when_the_executor_is_not_pid1(self):
        # 不能用真实的 pid：在没有 init 的容器里直接跑测试时，测试进程自己
        # 就是 PID 1。
        with mock.patch.object(proc_init.os, "getpid", return_value=4242), \
                mock.patch.object(proc_init, "split") as split:
            proc_init.split_if_pid1()
        split.assert_not_called()

    @unittest.skipUnless(POSIX, "PID 1 only exists as a concept on POSIX")
    def test_pid1_splits(self):
        # fork 之后子进程的 pid 不再是 1：getpid 先答 1，之后答 7。
        with mock.patch.object(proc_init.os, "getpid",
                               side_effect=[1, 7, 7]), \
                mock.patch.object(proc_init, "split") as split, \
                mock.patch("builtins.print") as log:
            proc_init.split_if_pid1()
        split.assert_called_once_with()
        self.assertIn("continues as pid 7", log.call_args.args[0])

    @unittest.skipUnless(POSIX, "PID 1 only exists as a concept on POSIX")
    def test_failed_fork_leaves_the_executor_running_as_pid1(self):
        # split() 在 fork 失败时原样返回（自己已经打过警告）：还是 PID 1，
        # 不能再谎报「init 已就位」。
        with mock.patch.object(proc_init.os, "getpid", return_value=1), \
                mock.patch.object(proc_init, "split") as split, \
                mock.patch("builtins.print") as log:
            proc_init.split_if_pid1()
        split.assert_called_once_with()
        log.assert_not_called()

    def test_windows_never_splits(self):
        with mock.patch.object(proc_init.os, "name", "nt"), \
                mock.patch.object(proc_init.os, "getpid", return_value=1):
            self.assertFalse(proc_init.is_pid1())

    @unittest.skipUnless(POSIX, "wait statuses are POSIX")
    def test_exit_code_follows_the_shell_convention(self):
        self.assertEqual(proc_init.exit_code_of(0), 0)
        self.assertEqual(proc_init.exit_code_of(3 << 8), 3)
        self.assertEqual(proc_init.exit_code_of(int(signal.SIGKILL)), 137)
        self.assertEqual(proc_init.exit_code_of(int(signal.SIGTERM)), 143)

    def test_serve_splits_before_the_executor_starts_any_thread(self):
        order: list[str] = []

        class FakeExecutor:
            exit_code = 7

            def __init__(self, config, **kwargs):
                order.append("executor")

            def run(self):
                order.append("run")

        with mock.patch.object(executor_module.proc_init, "split_if_pid1",
                               side_effect=lambda: order.append("split")), \
                mock.patch.object(executor_module, "Executor", FakeExecutor):
            code = executor_module.serve(object(), once=True, job_id="job-1")
        self.assertEqual(order, ["split", "executor", "run"])
        self.assertEqual(code, 7)


@unittest.skipUnless(POSIX, "fork / signals are POSIX")
class InitProcessTests(unittest.TestCase):
    """真的 fork：父进程当 init，子进程继续往下跑。"""

    def test_child_continues_and_its_exit_code_is_propagated(self):
        done = run_script(
            "import os, sys\n"
            "from calltable import proc_init\n"
            "parent = os.getpid()\n"
            "proc_init.split()\n"
            "print('child' if os.getppid() == parent else 'init', flush=True)\n"
            "sys.exit(7)\n")
        self.assertEqual(done.returncode, 7, done.stderr)
        # 只有子进程从 split() 返回：这一行恰好出现一次。
        self.assertEqual(done.stdout.split(), ["child"], done.stderr)

    def test_child_killed_by_a_signal_becomes_128_plus_n(self):
        done = run_script(
            "import os, signal\n"
            "from calltable import proc_init\n"
            "proc_init.split()\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n")
        self.assertEqual(done.returncode, 128 + int(signal.SIGKILL), done.stderr)

    def test_signals_sent_to_the_init_reach_the_executor(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        script = (
            "import signal, sys, time\n"
            "from calltable import proc_init\n"
            "proc_init.split()\n"
            "def on_term(signum, frame):\n"
            "    print('got', signal.Signals(signum).name, flush=True)\n"
            "    sys.exit(9)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n")
        helper = subprocess.Popen([sys.executable, "-c", script],
                                  cwd=str(REPO_ROOT), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        try:
            self.assertEqual(helper.stdout.readline().strip(), "ready")
            # helper.pid 是留下当 init 的父进程；处理函数装在子进程里。
            helper.send_signal(signal.SIGTERM)
            out, err = helper.communicate(timeout=30)
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.communicate()
        self.assertEqual(out.strip(), "got SIGTERM", err)
        self.assertEqual(helper.returncode, 9, err)

    def test_interrupt_is_delivered_once_as_keyboard_interrupt(self):
        """SIGINT 走执行器原有的 KeyboardInterrupt 收尾路径。"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        script = (
            "import sys, time\n"
            "from calltable import proc_init\n"
            "proc_init.split()\n"
            "print('ready', flush=True)\n"
            "try:\n"
            "    time.sleep(60)\n"
            "except KeyboardInterrupt:\n"
            "    print('interrupted', flush=True)\n"
            "    sys.exit(4)\n")
        helper = subprocess.Popen([sys.executable, "-c", script],
                                  cwd=str(REPO_ROOT), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        try:
            self.assertEqual(helper.stdout.readline().strip(), "ready")
            helper.send_signal(signal.SIGINT)
            out, err = helper.communicate(timeout=30)
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.communicate()
        self.assertEqual(out.strip(), "interrupted", err)
        self.assertEqual(helper.returncode, 4, err)


@unittest.skipUnless(LINUX, "child subreaper and /proc are Linux-only")
class OrphanReapingTests(unittest.TestCase):
    """孤儿进程被收养之后有没有人 wait。"""

    def _subreaper(self, mode: str) -> dict[str, str]:
        done = run_script(SUBREAPER_SCRIPT, mode)
        if done.returncode == 77:
            self.skipTest("prctl(PR_SET_CHILD_SUBREAPER) is not available here")
        self.assertEqual(done.returncode, 5, done.stdout + done.stderr)
        fields: dict[str, str] = {}
        for token in done.stdout.split():
            key, _, value = token.partition("=")
            fields[key] = value
        return fields

    def test_init_reaps_orphans_adopted_from_the_job_tree(self):
        fields = self._subreaper("init")
        # 孤儿确实过继给了留下当 init 的那个进程，而不是系统的 init。
        self.assertNotEqual(fields["self"], fields["adopter"])
        self.assertEqual(fields["reaper"], fields["adopter"], fields)
        self.assertEqual(fields["orphan"], "reaped", fields)

    def test_without_the_init_an_adopted_orphan_stays_a_zombie(self):
        """对照组：收养者不 wait 时孤儿就是僵尸（修复前容器里的现象）。"""
        fields = self._subreaper("plain")
        self.assertEqual(fields["self"], fields["adopter"])
        self.assertEqual(fields["reaper"], fields["adopter"], fields)
        self.assertEqual(fields["orphan"], "zombie", fields)

    def test_real_pid1_splits_and_reaps(self):
        if not can_unshare_pid1():
            self.skipTest("unprivileged user + PID namespaces are not "
                          "available on this host")
        done = run_script(PID1_SCRIPT, prefix=UNSHARE_PID1)
        self.assertEqual(done.returncode, 5, done.stdout + done.stderr)
        fields = dict(token.partition("=")[::2] for token in done.stdout.split())
        self.assertEqual(fields["before"], "1", fields)
        self.assertNotEqual(fields["self"], "1", fields)
        self.assertEqual(fields["parent"], "1", fields)
        self.assertEqual(fields["reaper"], "1", fields)
        self.assertEqual(fields["orphan"], "reaped", fields)
        self.assertIn("started as PID 1 without an init", done.stdout)


if __name__ == "__main__":
    unittest.main()
