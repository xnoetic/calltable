"""bin/ci-local：参数解析、结果判定与汇总。

只测纯逻辑：不连 hub、不提交任务、不启动 ct。干跑（``main(["--dry-run"])``）
也只读本地 git 状态。
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "bin" / "ci-local"


def load_ci_local():
    """把无扩展名的 bin/ci-local 当模块加载（它就是普通 Python 源码）。"""
    name = "calltable_bin_ci_local"
    loader = importlib.machinery.SourceFileLoader(name, str(_SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass 等依赖 __module__ 的查找
    loader.exec_module(module)
    return module


ci_local = load_ci_local()

SAMPLE_LOG = (
    "queued job-20261124141216-64afed\n"
    "  project=calltable host=devpc slot=heavy\n"
    "queued 2m41s, 3 ahead, est 6m\n"
    "== python ==\n"
    "Python 3.12.3\n"
    "== unittest ==\n"
    "test_one (tests.test_one.One.test_one) ... ok\n"
    "Ran 1234 tests in 118.2s\n"
    "\n"
    "OK\n"
    "== calltable --version ==\n"
    "0.3.0\n"
    "calltable-local-ci: ok\n"
    "[succeeded] exit=0 duration=1m58s\n"
)


class HostParsingTests(unittest.TestCase):
    def test_default_hosts(self):
        hosts = ci_local.parse_hosts(ci_local.DEFAULT_HOSTS)
        self.assertEqual([(h.name, h.python) for h in hosts],
                         [("devpc", "python3"), ("yiqipc", "python")])

    def test_single_host_uses_default_python(self):
        hosts = ci_local.parse_hosts("devpc")
        self.assertEqual([(h.name, h.python) for h in hosts],
                         [("devpc", "python3")])

    def test_explicit_python_overrides_default(self):
        hosts = ci_local.parse_hosts(" devpc=python3.13 , yiqipc = py ")
        self.assertEqual([(h.name, h.python) for h in hosts],
                         [("devpc", "python3.13"), ("yiqipc", "py")])

    def test_unknown_host_gets_a_default(self):
        hosts = ci_local.parse_hosts("lazyprod")
        self.assertEqual(hosts[0].python,
                         ci_local.UNKNOWN_HOST_PYTHON)

    def test_duplicate_host_is_rejected(self):
        with self.assertRaises(ValueError):
            ci_local.parse_hosts("devpc,devpc=python")

    def test_empty_spec_is_rejected(self):
        for spec in ("", "   ", " , "):
            with self.assertRaises(ValueError):
                ci_local.parse_hosts(spec)

    def test_empty_python_is_rejected(self):
        with self.assertRaises(ValueError):
            ci_local.parse_hosts("devpc=")


class DurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(ci_local.parse_duration("30m"), 1800)
        self.assertEqual(ci_local.parse_duration("1h"), 3600)
        self.assertEqual(ci_local.parse_duration("90s"), 90)
        self.assertEqual(ci_local.parse_duration("120"), 120)
        self.assertEqual(ci_local.parse_duration(" 2H "), 7200)

    def test_invalid(self):
        for value in ("soon", "30 minutes", "", "m30"):
            with self.assertRaises(ValueError):
                ci_local.parse_duration(value)


class CommandTests(unittest.TestCase):
    def test_remote_script_runs_the_three_checks(self):
        script = ci_local.remote_script("python")
        lines = script.splitlines()
        self.assertEqual(lines[0], "set -e")
        self.assertIn('"$PY" --version', script)
        self.assertIn('"$PY" -m unittest discover -s tests -t . -v', script)
        self.assertIn('"$PY" -m calltable --version', script)
        # 完成标记必须是最后一行：中途失败就永远打不出来。
        self.assertIn(ci_local.OK_MARKER, lines[-1])

    def test_remote_script_quotes_python_paths_with_spaces(self):
        script = ci_local.remote_script("/opt/my python/bin/python3")
        self.assertIn("PY='/opt/my python/bin/python3'", script)

    def test_ct_command_shape(self):
        host = ci_local.Host(name="devpc", python="python3")
        command = ci_local.ct_command(
            host, project="calltable", slot="heavy", timeout="30m",
            label="ci-local abc1234 devpc")
        self.assertEqual(command[:3], [sys.executable, "-m", "calltable"])
        self.assertEqual(command[command.index("--host") + 1], "devpc")
        self.assertEqual(command[command.index("--project") + 1], "calltable")
        self.assertEqual(command[command.index("--timeout") + 1], "30m")
        self.assertEqual(command[command.index("--label") + 1],
                         "ci-local abc1234 devpc")
        self.assertIn("--wait", command)
        self.assertIn("--no-lock", command)
        self.assertIn("--yes", command)
        self.assertEqual(command[-3:],
                         ["bash", "-c", ci_local.remote_script("python3")])

    def test_label_marks_a_dirty_worktree(self):
        self.assertEqual(ci_local.build_label("abc1234", False, "devpc"),
                         "ci-local abc1234 devpc")
        self.assertEqual(ci_local.build_label("abc1234", True, "yiqipc"),
                         "ci-local abc1234+dirty yiqipc")


class LogParsingTests(unittest.TestCase):
    def test_job_id(self):
        self.assertEqual(ci_local.parse_job_id(SAMPLE_LOG),
                         "job-20261124141216-64afed")

    def test_job_id_accepts_already_queued(self):
        self.assertEqual(ci_local.parse_job_id("already queued job-1\n"),
                         "job-1")

    def test_queue_progress_is_not_a_job_id(self):
        self.assertIsNone(ci_local.parse_job_id(
            "queued 2m41s, 3 ahead, est 6m\n"))

    def test_test_count(self):
        self.assertEqual(ci_local.parse_test_count(SAMPLE_LOG), 1234)
        self.assertEqual(ci_local.parse_test_count("Ran 1 test in 0.001s\n"), 1)
        self.assertIsNone(ci_local.parse_test_count("no tests here\n"))


class EvaluateTests(unittest.TestCase):
    def test_success(self):
        ok, tests, note = ci_local.evaluate(SAMPLE_LOG, 0)
        self.assertTrue(ok)
        self.assertEqual(tests, 1234)
        self.assertEqual(note, "")

    def test_failure_keeps_the_test_count(self):
        ok, tests, note = ci_local.evaluate(SAMPLE_LOG, 1)
        self.assertFalse(ok)
        self.assertEqual(tests, 1234)
        self.assertIn("1", note)

    def test_missing_completion_marker_fails_even_with_exit_zero(self):
        text = SAMPLE_LOG.replace(ci_local.OK_MARKER, "")
        ok, _, note = ci_local.evaluate(text, 0)
        self.assertFalse(ok)
        self.assertIn("marker", note)

    def test_zero_tests_fails(self):
        text = "Ran 0 tests in 0.000s\n\nOK\n" + ci_local.OK_MARKER + "\n"
        ok, tests, note = ci_local.evaluate(text, 0)
        self.assertFalse(ok)
        self.assertEqual(tests, 0)
        self.assertIn("no tests", note)

    def test_missing_test_count_fails(self):
        text = "Python 3.12.3\n" + ci_local.OK_MARKER + "\n"
        ok, tests, _ = ci_local.evaluate(text, 0)
        self.assertFalse(ok)
        self.assertIsNone(tests)

    def test_local_wait_timeout_has_its_own_message(self):
        ok, _, note = ci_local.evaluate(SAMPLE_LOG, 75)
        self.assertFalse(ok)
        self.assertIn("timed out", note)

    def test_job_timeout(self):
        ok, _, note = ci_local.evaluate(SAMPLE_LOG, 124)
        self.assertFalse(ok)
        self.assertIn("timeout", note)

    def test_cancelled(self):
        ok, _, note = ci_local.evaluate(SAMPLE_LOG, 130)
        self.assertFalse(ok)
        self.assertIn("cancelled", note)


class SummaryTests(unittest.TestCase):
    def results(self):
        run_dir = _REPO_ROOT / ".ci-local" / "20261124-223512-abc1234"
        return [
            ci_local.HostResult(host="devpc", log_path=run_dir / "devpc.log",
                                job_id="job-1", tests=1234, ok=True,
                                elapsed=118.0),
            ci_local.HostResult(host="yiqipc", log_path=run_dir / "yiqipc.log",
                                job_id="job-2", tests=1230, ok=False,
                                note="command exited 1", elapsed=301.0),
        ]

    def test_summary_shows_every_host_and_failure(self):
        text = ci_local.format_summary(self.results(), _REPO_ROOT)
        self.assertIn("devpc", text)
        self.assertIn("yiqipc", text)
        self.assertIn("job-1", text)
        self.assertIn("1234", text)
        self.assertIn("ok", text)
        self.assertIn("FAIL", text)
        self.assertIn(".ci-local/20261124-223512-abc1234/devpc.log", text)
        self.assertIn("yiqipc: command exited 1", text)
        self.assertIn("FAILED: yiqipc", text)

    def test_summary_reports_success(self):
        text = ci_local.format_summary(self.results()[:1], _REPO_ROOT)
        self.assertIn("all hosts passed", text)
        self.assertNotIn("FAILED:", text)

    def test_elapsed_format(self):
        self.assertEqual(ci_local.format_elapsed(12), "12s")
        self.assertEqual(ci_local.format_elapsed(118), "1m58s")
        self.assertEqual(ci_local.format_elapsed(3720), "1h02m")


class DryRunTests(unittest.TestCase):
    def test_dry_run_prints_commands_without_submitting(self):
        if not (_REPO_ROOT / ".git").exists():
            self.skipTest("not a git checkout; the label needs a HEAD sha")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ci_local.main(["--dry-run", "--hosts", "devpc=python3"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("[devpc] python=python3", text)
        self.assertIn("--wait", text)
        self.assertIn("unittest discover", text)
        # 干跑不提交：ct 的 "queued <job>" 行不该出现。
        self.assertNotIn(" queued job-", text)


if __name__ == "__main__":
    unittest.main()
