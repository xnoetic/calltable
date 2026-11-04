"""取消/超时必须杀干净整棵进程树，不能留孤儿。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from pathlib import Path

from calltable import proc, proc_linux

SCRIPT = textwrap.dedent("""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    print(child.pid, flush=True)
    print("ready", flush=True)
    time.sleep(300)
""")


class ProcessTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.script = Path(self.tmp.name) / "tree.py"
        self.script.write_text(SCRIPT, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_terminate_kills_parent_and_grandchild(self):
        tree = proc.spawn(argv=[sys.executable, str(self.script)],
                          cwd=self.tmp.name, env={},
                          job_id="test-tree", use_systemd=False)
        try:
            first = tree.popen.stdout.readline().decode().strip()
            second = tree.popen.stdout.readline().decode().strip()
            self.assertEqual(second, "ready")
            child_pid = int(first)
            self.assertTrue(proc.pid_alive(tree.popen.pid))
            self.assertTrue(proc.pid_alive(child_pid))

            tree.terminate(grace=2)

            deadline = time.time() + 15
            while time.time() < deadline:
                if not proc.pid_alive(tree.popen.pid) and not proc.pid_alive(child_pid):
                    break
                time.sleep(0.2)
            self.assertFalse(proc.pid_alive(tree.popen.pid),
                             "parent process survived terminate()")
            self.assertFalse(proc.pid_alive(child_pid),
                             "grandchild process survived terminate()")
        finally:
            tree.close()

    def test_sample_reports_cpu_and_memory(self):
        tree = proc.spawn(argv=[sys.executable, "-c",
                                 "import time\n"
                                 "x = bytearray(8 * 1024 * 1024)\n"
                                 "time.sleep(30)\n"],
                          cwd=self.tmp.name, env={},
                          job_id="test-sample", use_systemd=False)
        try:
            deadline = time.time() + 15
            sample = None
            while time.time() < deadline:
                sample = tree.sample()
                if sample.mem_peak > 0:
                    break
                time.sleep(0.5)
            self.assertIsNotNone(sample)
            self.assertGreater(sample.mem_peak, 0, "no memory sample collected")
            first_cpu = sample.cpu_usec
            time.sleep(0.4)
            later = tree.sample()
            self.assertGreaterEqual(later.cpu_usec, first_cpu,
                                    "CPU counter went backwards")
            self.assertGreaterEqual(later.mem_peak, 0)
        finally:
            tree.terminate(2)
            tree.close()
            self.tmp.cleanup()


class DirectTreeTests(unittest.TestCase):
    """无 systemd 的 direct 模式：采样整个进程树 + 软内存上限。"""

    TABLE = {
        # pid: (ppid, session)
        100: (1, 100),      # 任务主进程（setsid 后 session == pid）
        101: (100, 100),    # 子进程
        102: (101, 100),    # 孙进程
        200: (1, 1),        # 无关进程
    }

    def _impl(self, limit=None):
        impl = proc_linux.DirectImpl(100, soft_limit_bytes=limit)
        return impl

    def _patch(self, impl, rss):
        table = {pid: {"state": "S", "ppid": ppid, "session": session,
                       "utime": 10 * pid, "stime": 5 * pid}
                 for pid, (ppid, session) in self.TABLE.items()}

        def rss_of(pid):
            return rss.get(pid, 0)

        impl._proc_table = lambda: table
        impl._rss_bytes = rss_of
        return impl

    def test_descendants_are_found_by_session_and_parent_chain(self):
        rss = {100: 1 << 20, 101: 2 << 20, 102: 4 << 20, 200: 8 << 20}
        impl = self._patch(self._impl(), rss)
        self.assertEqual(sorted(impl._pids()), [100, 101, 102])
        sample = impl.sample()
        self.assertEqual(sample.mem_current, 7 << 20)
        self.assertEqual(sample.mem_peak, 7 << 20)
        self.assertEqual(sample.tasks, 3)
        ticks = sum(10 * pid + 5 * pid for pid in (100, 101, 102))
        self.assertEqual(sample.cpu_usec,
                         int(ticks * 1_000_000 / impl.CLK_TCK))

    def test_detached_child_is_still_counted(self):
        # 子进程自己 setsid（session == 自己）时靠父子闭包兜住。
        table = {
            100: {"state": "S", "ppid": 1, "session": 100, "utime": 1, "stime": 1},
            103: {"state": "S", "ppid": 100, "session": 103, "utime": 1, "stime": 1},
        }
        impl = proc_linux.DirectImpl(100)
        impl._proc_table = lambda: table
        impl._rss_bytes = lambda pid: 1 << 20
        self.assertEqual(sorted(impl._pids()), [100, 103])
        self.assertEqual(impl.sample().mem_current, 2 << 20)

    def test_cpu_counter_never_goes_backwards_when_children_exit(self):
        impl = proc_linux.DirectImpl(100)
        impl._proc_table = lambda: {
            100: {"state": "S", "ppid": 1, "session": 100, "utime": 100, "stime": 0},
            101: {"state": "S", "ppid": 100, "session": 100, "utime": 50, "stime": 0},
        }
        impl._rss_bytes = lambda pid: 0
        first = impl.sample()
        impl._proc_table = lambda: {
            100: {"state": "S", "ppid": 1, "session": 100, "utime": 100, "stime": 0},
        }
        second = impl.sample()
        self.assertEqual(second.cpu_usec, first.cpu_usec)

    def test_soft_limit_needs_two_consecutive_over_limit_samples(self):
        high = {100: 100 << 20}
        low = {100: 1 << 20}
        impl = proc_linux.DirectImpl(100, soft_limit_bytes=50 << 20)
        impl._proc_table = lambda: {
            100: {"state": "S", "ppid": 1, "session": 100, "utime": 0, "stime": 0}}
        impl._rss_bytes = lambda pid: high.get(pid, 0)
        impl.sample()
        self.assertFalse(impl.exceeded)
        impl.sample()
        self.assertTrue(impl.exceeded)

    def test_soft_limit_resets_after_a_sample_under_the_limit(self):
        impl = proc_linux.DirectImpl(100, soft_limit_bytes=50 << 20)
        impl._proc_table = lambda: {
            100: {"state": "S", "ppid": 1, "session": 100, "utime": 0, "stime": 0}}
        values = iter([100 << 20, 1 << 20, 100 << 20, 100 << 20])
        impl._rss_bytes = lambda pid: next(values)
        for _ in range(3):
            impl.sample()
        self.assertFalse(impl.exceeded)
        impl.sample()
        self.assertTrue(impl.exceeded)

    def test_no_limit_never_exceeds(self):
        impl = proc_linux.DirectImpl(100)
        impl._proc_table = lambda: {
            100: {"state": "S", "ppid": 1, "session": 100, "utime": 0, "stime": 0}}
        impl._rss_bytes = lambda pid: 100 << 30
        for _ in range(4):
            impl.sample()
        self.assertFalse(impl.exceeded)

    @unittest.skipIf(__import__("os").name == "nt", "POSIX /proc only")
    def test_real_process_tree_memory_includes_children(self):
        script = textwrap.dedent("""
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c",
                "import time; x = bytearray(24*1024*1024); time.sleep(30)"])
            print(child.pid, flush=True)
            time.sleep(30)
        """)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "tree.py"
            path.write_text(script, encoding="utf-8")
            tree = proc.spawn(argv=[sys.executable, str(path)], cwd=tmp,
                              env={}, job_id="tree-mem", use_systemd=False)
            try:
                tree.popen.stdout.readline()
                deadline = time.time() + 15
                sample = None
                while time.time() < deadline:
                    sample = tree.sample()
                    if sample.mem_current > 20 * 1024 * 1024:
                        break
                    time.sleep(0.4)
                self.assertIsNotNone(sample)
                self.assertGreater(sample.mem_current, 20 * 1024 * 1024,
                                   "child memory was not counted")
                self.assertGreaterEqual(sample.tasks, 2)
            finally:
                tree.terminate(2)
                tree.close()


class SystemdParseTests(unittest.TestCase):
    """B2：采样只解析 systemctl show 的属性，不读 cgroup 文件。"""

    NL = chr(10)

    def test_parse_systemd_show(self):
        text = self.NL.join([
            "MemoryCurrent=104857600",
            "MemoryPeak=209715200",
            "CPUUsageNSec=2500000000",
            "TasksCurrent=7",
            "",
        ])
        sample = proc_linux.parse_systemd_show(text)
        self.assertEqual(sample.mem_current, 104857600)
        self.assertEqual(sample.mem_peak, 209715200)
        self.assertEqual(sample.cpu_usec, 2_500_000)
        self.assertEqual(sample.tasks, 7)

    def test_parse_ignores_infinity_and_garbage(self):
        sample = proc_linux.parse_systemd_show(self.NL.join([
            "MemoryCurrent=[not set]",
            "MemoryPeak=infinity",
            "CPUUsageNSec=-1",
            "TasksCurrent=abc",
            "",
        ]))
        self.assertEqual(sample.mem_current, 0)
        self.assertEqual(sample.mem_peak, 0)
        self.assertEqual(sample.cpu_usec, 0)

    def test_peak_falls_back_to_current(self):
        sample = proc_linux.parse_systemd_show(self.NL.join([
            "MemoryCurrent=4096",
            "MemoryPeak=[not set]",
            "",
        ]))
        self.assertEqual(sample.mem_peak, 4096)

    def test_probe_reports_reason(self):
        available, reason = proc.probe_systemd()
        self.assertIsInstance(available, bool)
        self.assertTrue(reason)
        if __import__("os").name == "nt":
            self.assertFalse(available)


class SystemdLifecycleTests(unittest.TestCase):
    """B2：systemctl kill 的返回码要检查，且始终有 killpg 兜底。"""

    def setUp(self):
        self.impl = proc_linux.SystemdImpl("calltable-x.scope", 4321)

    def _completed(self, rc, stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=rc,
                                           stdout="", stderr=stderr)

    def test_terminate_falls_back_to_killpg_even_when_systemctl_fails(self):
        with mock.patch("calltable.proc_linux._systemd_run",
                        return_value=self._completed(1, "no such unit")) as run,                 mock.patch("calltable.proc_linux.os.killpg", create=True) as killpg:
            self.impl.terminate(0.05)
        self.assertTrue(run.called)
        killpg.assert_called_with(4321, proc_linux.SIGKILL)
        flattened = [str(part) for call in run.call_args_list for part in call.args[0]]
        self.assertIn("SIGKILL", flattened)

    def test_close_always_kills_scope_then_resets_failed(self):
        with mock.patch("calltable.proc_linux._systemd_run",
                        return_value=self._completed(0)) as run:
            self.impl.close()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn("kill", commands[0])
        self.assertIn("SIGKILL", commands[0])
        self.assertIn("reset-failed", commands[1])
        # systemctl 必须拿到带 .scope 后缀的完整 unit
        self.assertTrue(all(any(str(a).endswith(".scope") for a in cmd)
                            for cmd in commands), commands)

    def test_sample_queries_the_scope_unit(self):
        payload = "".join(["MemoryCurrent=1024\n", "MemoryPeak=2048\n",
                            "CPUUsageNSec=3000\n", "TasksCurrent=1\n"])
        with mock.patch("calltable.proc_linux._systemd_run",
                        return_value=subprocess.CompletedProcess(
                            args=[], returncode=0, stdout=payload,
                            stderr="")) as run:
            sample = self.impl.sample()
        self.assertEqual(sample.mem_current, 1024)
        self.assertEqual(sample.cpu_usec, 3)
        argv = run.call_args.args[0]
        self.assertTrue(argv[-1].endswith(".scope"), argv)

    def test_probe_true_when_systemd_run_succeeds(self):
        with mock.patch("calltable.proc_linux.shutil.which", return_value="/usr/bin/systemd-run"),                 mock.patch("calltable.proc_linux._systemd_run",
                           return_value=self._completed(0)):
            self.assertEqual(proc_linux.probe(), (True, "ok"))

    def test_probe_false_with_reason_when_systemd_run_fails(self):
        with mock.patch("calltable.proc_linux.shutil.which", return_value="/usr/bin/systemd-run"),                 mock.patch("calltable.proc_linux._systemd_run",
                           return_value=self._completed(1, "Failed to connect to bus")):
            available, reason = proc_linux.probe()
        self.assertFalse(available)
        self.assertIn("Failed to connect", reason)

    def test_spawn_passes_collect_quiet_limits_and_slice(self):
        fake = mock.Mock(pid=999, stdout=None)
        with mock.patch("calltable.proc_linux.subprocess.Popen", return_value=fake) as popen:
            proc_linux.spawn_systemd(argv=["bash", "run.sh"], cwd=".",
                                     env={}, job_id="job-1",
                                     memory_max="20G", cpu_weight="400",
                                     slice="calltable-heavy.slice")
        argv = popen.call_args.args[0]
        self.assertIn("--collect", argv)
        self.assertIn("--quiet", argv)
        self.assertIn("--slice", argv)
        self.assertIn("calltable-heavy.slice", argv)
        self.assertIn("MemoryMax=20G", argv)
        self.assertIn("CPUWeight=400", argv)
        self.assertIn("calltable-job-1", argv)

    @unittest.skipUnless(__import__("os").name == "nt", "windows only")
    def test_job_object_cpu_rate_and_normal_priority(self):
        import ctypes
        from calltable import proc_windows

        kernel32 = proc_windows.kernel32
        kernel32.GetPriorityClass.restype = ctypes.wintypes.DWORD
        kernel32.GetPriorityClass.argtypes = [ctypes.wintypes.HANDLE]

        tree = proc.spawn(argv=[sys.executable, "-c", "import time; time.sleep(5)"],
                          cwd=tempfile.gettempdir(), env={}, job_id="rate",
                          use_systemd=False, cpu_rate_percent=55)
        try:
            handle = tree._impl.handle
            info = proc_windows.JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            ok = kernel32.QueryInformationJobObject(
                handle, proc_windows.JobObjectCpuRateControlInformation,
                ctypes.byref(info), ctypes.sizeof(info), None)
            self.assertTrue(ok)
            self.assertTrue(info.ControlFlags
                            & proc_windows.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP)
            self.assertEqual(info.CpuRate, 5500)

            process = kernel32.OpenProcess(0x1000, False, tree.popen.pid)
            self.assertTrue(process)
            try:
                self.assertEqual(kernel32.GetPriorityClass(process),
                                 proc_windows.NORMAL_PRIORITY_CLASS)
            finally:
                kernel32.CloseHandle(process)
        finally:
            tree.terminate(2)
            tree.close()


if __name__ == "__main__":
    unittest.main()
