"""仓库自带的项目声明 ``.calltable.json``（issue #3）。

解析与校验、优先级（命令行 > 声明 > hub profile）、``ct run <任务名>``、
``--dry-run`` 的来源说明，以及对进程内 hub 的真实提交。
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from calltable import cli, repoconf
from calltable.hub import Handler, Hub, HubServer
from tests.helpers import make_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


def make_repo(root: Path, declaration=None) -> Path:
    repo = Path(root) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    if declaration is not None:
        write_declaration(repo, declaration)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def write_declaration(repo: Path, declaration) -> None:
    text = (declaration if isinstance(declaration, str)
            else json.dumps(declaration, indent=2))
    (Path(repo) / repoconf.DECLARATION_FILE).write_text(text, encoding="utf-8")


def run_cli(*argv) -> tuple[int, str, str]:
    """跑 ``cli run``；argparse 的 SystemExit 也折算成退出码。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(["cli", "run", *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


FULL = {
    "project": "demo",
    "host": "h1",
    "slot": "light",
    "locks": ["repo-lock"],
    "timeout": "30m",
    "artifacts": ["reports/*.xml"],
    "rules": [{"match": "integration", "slot": "heavy",
               "locks": ["db@{host}"], "timeout": "2h"}],
    "tasks": {"verify": ["python", "-c", "print('verify')"],
              "it": ["pytest", "-k", "integration"]},
}


class ParseTests(unittest.TestCase):
    def parse(self, data):
        return repoconf.parse_declaration(data, Path(repoconf.DECLARATION_FILE))

    def assertFieldError(self, data, field, *fragments):
        with self.assertRaises(repoconf.DeclarationError) as ctx:
            self.parse(data)
        self.assertEqual(ctx.exception.field, field)
        self.assertIn(repr(field), str(ctx.exception))
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))
        return ctx.exception

    def test_full_declaration(self):
        decl = self.parse(FULL)
        self.assertEqual(decl.project, "demo")
        self.assertEqual(decl.host, "h1")
        self.assertIsNone(decl.platform)
        self.assertEqual(decl.slot, "light")
        self.assertEqual(decl.locks, ["repo-lock"])
        self.assertEqual(decl.timeout, 1800)
        self.assertEqual(decl.artifacts, ["reports/*.xml"])
        self.assertEqual(len(decl.rules), 1)
        self.assertEqual(decl.rules[0].slot, "heavy")
        self.assertEqual(decl.rules[0].timeout, 7200)
        self.assertEqual(decl.rules[0].locks, ["db@{host}"])
        self.assertEqual(decl.tasks["it"], ["pytest", "-k", "integration"])

    def test_missing_optional_fields_stay_unset(self):
        decl = self.parse({"project": "demo"})
        self.assertIsNone(decl.host)
        self.assertIsNone(decl.slot)
        self.assertIsNone(decl.locks)
        self.assertIsNone(decl.timeout)
        self.assertIsNone(decl.artifacts)
        self.assertEqual(decl.rules, [])
        self.assertEqual(decl.tasks, {})
        # 规则里没写 locks 也保持「沿用」语义（和 profile 一样）
        rule = self.parse({"rules": [{"match": "x"}]}).rules[0]
        self.assertIsNone(rule.locks)

    def test_platform(self):
        self.assertEqual(self.parse({"platform": "Linux"}).platform, "linux")
        self.assertFieldError({"platform": "beos"}, "platform", "linux")
        self.assertFieldError({"host": "h1", "platform": "linux"}, "platform",
                              "not both")

    def test_malformed_fields_name_the_field(self):
        cases = [
            ({"project": 3}, "project"),
            ({"project": "bad name!"}, "project"),
            ({"host": ""}, "host"),
            ({"slot": ["heavy"]}, "slot"),
            ({"locks": "one"}, "locks"),
            ({"locks": ["ok", "bad lock"]}, "locks[1]"),
            ({"locks": ["a", "a"]}, "locks"),
            ({"timeout": "soon"}, "timeout"),
            ({"timeout": True}, "timeout"),
            ({"timeout": 0}, "timeout"),
            ({"artifacts": "x"}, "artifacts"),
            ({"artifacts": ["ok/*", "../up"]}, "artifacts[1]"),
            ({"artifacts": ["/etc/passwd"]}, "artifacts[0]"),
            ({"artifacts": ["C:\\x"]}, "artifacts[0]"),
            ({"rules": {}}, "rules"),
            ({"rules": ["x"]}, "rules[0]"),
            ({"rules": [{"slot": "heavy"}]}, "rules[0].match"),
            ({"rules": [{"match": "("}]}, "rules[0].match"),
            ({"rules": [{"match": "a"}, {"match": "b", "timeout": "zz"}]},
             "rules[1].timeout"),
            ({"rules": [{"match": "a", "colour": "red"}]}, "rules[0].colour"),
            ({"tasks": []}, "tasks"),
            ({"tasks": {"verify": "make verify"}}, "tasks.verify"),
            ({"tasks": {"verify": []}}, "tasks.verify"),
            ({"tasks": {"-x": ["true"]}}, "tasks.-x"),
            ({"hots": "h1"}, "hots"),
        ]
        for data, field in cases:
            with self.subTest(data=data):
                self.assertFieldError(data, field)

    def test_not_an_object(self):
        with self.assertRaises(repoconf.DeclarationError):
            self.parse(["project", "demo"])

    def test_host_only_fields_are_rejected_with_a_reason(self):
        for name in repoconf.HOST_ONLY_FIELDS:
            with self.subTest(field=name):
                error = self.assertFieldError({"project": "demo", name: "x"},
                                              name, "not allowed",
                                              "hub-side profile")
                self.assertIn(repoconf.HOST_ONLY_FIELDS[name], str(error))
        for name in ("env_files", "setup", "memory_max", "env"):
            self.assertIn(name, repoconf.HOST_ONLY_FIELDS)
        self.assertFieldError(
            {"rules": [{"match": "x", "memory_max": "4G"}]},
            "rules[0].memory_max", "not allowed", "memory limits")

    def test_load_from_repository_root(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp), {"project": "demo"})
            sub = repo / "sub"
            sub.mkdir()
            decl = repoconf.load_declaration(sub)
            self.assertEqual(decl.project, "demo")
            self.assertEqual(decl.path.name, repoconf.DECLARATION_FILE)
            write_declaration(repo, "{\"project\": ")
            with self.assertRaises(repoconf.DeclarationError) as ctx:
                repoconf.load_declaration(repo)
            self.assertIn("invalid JSON", str(ctx.exception))
            plain = Path(tmp) / "plain"
            plain.mkdir()
            self.assertIsNone(repoconf.load_declaration(plain))

    def test_this_repository_declares_the_calltable_project(self):
        """本仓库自带的声明：项目名与 bin/ci-local、hub profile 一致。"""
        path = REPO_ROOT / repoconf.DECLARATION_FILE
        if not path.is_file():
            self.skipTest("no .calltable.json in this checkout")
        decl = repoconf.parse_declaration(
            json.loads(path.read_text(encoding="utf-8")), path)
        self.assertEqual(decl.project, "calltable")
        ci_local = (REPO_ROOT / "bin" / "ci-local").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_PROJECT = "calltable"', ci_local)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.decl = repoconf.parse_declaration(FULL, Path("x"))

    def test_single_word_task_expands(self):
        self.assertEqual(repoconf.expand_task(self.decl, ["it"], False),
                         (["pytest", "-k", "integration"], "it"))

    def test_separator_forces_a_command(self):
        self.assertEqual(repoconf.expand_task(self.decl, ["it"], True),
                         (["it"], ""))

    def test_multi_word_and_unknown_are_commands(self):
        self.assertEqual(repoconf.expand_task(self.decl, ["it", "x"], False),
                         (["it", "x"], ""))
        self.assertEqual(repoconf.expand_task(self.decl, ["make"], False),
                         (["make"], ""))
        self.assertEqual(repoconf.expand_task(None, ["it"], False), (["it"], ""))


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.decl = repoconf.parse_declaration(FULL, Path("x"))

    def values(self, decl, cmd, **cli):
        options = dict(slot=None, locks=[], timeout=None, no_lock=False)
        options.update(cli)
        return repoconf.explicit_values(decl, cmd, **options)

    def test_without_declaration_only_cli_values_are_explicit(self):
        result = self.values(None, ["make"])
        self.assertEqual((result.slot, result.locks, result.no_lock,
                          result.timeout), (None, [], False, None))
        result = self.values(None, ["make"], slot="heavy", locks=["l"],
                             timeout=60, no_lock=True)
        self.assertEqual((result.slot, result.locks, result.no_lock,
                          result.timeout), ("heavy", ["l"], True, 60))

    def test_declaration_defaults_replace_the_hub_profile(self):
        result = self.values(self.decl, ["make"])
        self.assertEqual(result.slot, "light")
        self.assertEqual(result.timeout, 1800)
        # 声明的锁取代 hub profile 的默认锁
        self.assertEqual(result.locks, ["repo-lock"])
        self.assertTrue(result.no_lock)
        self.assertEqual(result.sources["slot"], ".calltable.json slot")

    def test_declaration_rule_beats_declaration_defaults(self):
        result = self.values(self.decl, ["pytest", "-k", "integration"])
        self.assertEqual(result.slot, "heavy")
        self.assertEqual(result.timeout, 7200)
        self.assertEqual(result.locks, ["db@{host}"])
        self.assertIn("rule 'integration'", result.sources["slot"])

    def test_cli_beats_declaration(self):
        result = self.values(self.decl, ["pytest", "-k", "integration"],
                             slot="light", timeout=60, locks=["extra"])
        self.assertEqual(result.slot, "light")
        self.assertEqual(result.timeout, 60)
        self.assertEqual(result.locks, ["db@{host}", "extra"])
        self.assertEqual(result.sources["slot"], "--slot")
        result = self.values(self.decl, ["make"], no_lock=True)
        self.assertEqual(result.locks, [])
        self.assertTrue(result.no_lock)

    def test_unset_declaration_fields_fall_through_to_the_hub(self):
        decl = repoconf.parse_declaration({"project": "demo"}, Path("x"))
        result = self.values(decl, ["make"])
        self.assertEqual((result.slot, result.locks, result.no_lock,
                          result.timeout), (None, [], False, None))

    def test_host_precedence(self):
        def no_inference(project, host, platform, profiles):
            return f"inferred:{platform}"

        self.assertEqual(repoconf.choose_host("demo", "h9", None, self.decl, {},
                                              no_inference)[0], "h9")
        self.assertEqual(repoconf.choose_host("demo", None, "windows", self.decl,
                                              {}, no_inference)[0],
                         "inferred:windows")
        self.assertEqual(repoconf.choose_host("demo", None, None, self.decl, {},
                                              no_inference),
                         ("h1", ".calltable.json host"))
        decl = repoconf.parse_declaration({"platform": "linux"}, Path("x"))
        self.assertEqual(repoconf.choose_host("demo", None, None, decl, {},
                                              no_inference)[0], "inferred:linux")
        self.assertEqual(repoconf.choose_host("demo", None, None, None, {},
                                              no_inference)[0], "inferred:None")


class DryRunDeclarationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.config = make_config(root / "cfg")
        self.repo = make_repo(root, FULL)

    def tearDown(self):
        self.tmp.cleanup()

    def dry(self, *argv):
        return run_cli("--config", str(self.config.path), "--repo",
                       str(self.repo), "--dry-run", *argv)

    def test_command_without_project_or_host(self):
        code, out, _ = self.dry("--", "make", "check")
        self.assertEqual(code, 0, out)
        self.assertIn("project=demo host=h1", out)
        self.assertIn("project   demo  <- .calltable.json project", out)
        self.assertIn("host      h1  <- .calltable.json host", out)
        self.assertIn("slot      light  <- .calltable.json slot", out)
        self.assertIn("locks     repo-lock  <- .calltable.json locks", out)
        self.assertIn("timeout   1800s  <- .calltable.json timeout", out)
        self.assertIn("artifacts reports/*.xml  <- .calltable.json artifacts", out)

    def test_task_name(self):
        code, out, _ = self.dry("it")
        self.assertEqual(code, 0, out)
        self.assertIn("command=pytest -k integration", out)
        self.assertIn("<- task 'it' in .calltable.json", out)
        self.assertIn("slot      heavy  <- .calltable.json rule 'integration'", out)

    def test_separator_keeps_a_task_name_as_a_command(self):
        code, out, _ = self.dry("--", "it")
        self.assertEqual(code, 0, out)
        self.assertIn("command=it\n", out)
        self.assertIn("command   it  <- command line", out)

    def test_unknown_single_word_runs_as_a_command_with_a_note(self):
        code, out, err = self.dry("verfy")
        self.assertEqual(code, 0, out)
        self.assertIn("command=verfy", out)
        self.assertIn("not a task", err)

    def test_cli_arguments_override_the_declaration(self):
        code, out, _ = self.dry("--project", "other", "--host", "h2",
                                "--slot", "heavy", "--timeout", "5m",
                                "--no-lock", "verify")
        self.assertEqual(code, 0, out)
        self.assertIn("project=other host=h2", out)
        self.assertIn("project   other  <- --project", out)
        self.assertIn("host      h2  <- --host", out)
        self.assertIn("slot      heavy  <- --slot", out)
        self.assertIn("timeout   300s  <- --timeout", out)
        self.assertIn("locks     -  <- --no-lock", out)

    def test_malformed_declaration_fails_before_submitting(self):
        write_declaration(self.repo, {"project": "demo",
                                      "rules": [{"match": "x", "timeout": "zz"}]})
        # 不是 dry-run：如果没在提交前报错，会去连 127.0.0.1:1 失败（退出码不是 2）
        code, out, err = run_cli("--config", str(self.config.path), "--repo",
                                 str(self.repo), "--", "true")
        self.assertEqual(code, 2)
        self.assertIn("'rules[0].timeout'", err)
        self.assertIn(".calltable.json", err)
        self.assertNotIn("queued", out)

    def test_host_only_field_is_rejected_before_submitting(self):
        write_declaration(self.repo, {"project": "demo", "setup": ["make deps"]})
        code, out, err = run_cli("--config", str(self.config.path), "--repo",
                                 str(self.repo), "--", "true")
        self.assertEqual(code, 2)
        self.assertIn("'setup'", err)
        self.assertIn("hub-side profile", err)

    def test_without_declaration_project_is_still_required(self):
        (self.repo / repoconf.DECLARATION_FILE).unlink()
        code, _, err = self.dry("--", "true")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: --project", err)
        code, out, _ = self.dry("--project", "demo", "--host", "h1", "--", "true")
        self.assertEqual(code, 0)
        self.assertIn("declaration=-", out)


TOKEN = "repoconf-token"


class SubmitWithDeclarationTests(unittest.TestCase):
    """对进程内 hub 真实提交：回执里的项目和主机来自声明。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.config = make_config(cls.root / "hub", hosts={
            "h1": {"slots": {"heavy": 1, "light": 2}},
            "h2": {"slots": {"heavy": 1, "light": 1}}})
        cls.config.raw["token"] = TOKEN
        cls.config.token = TOKEN
        # hub 侧 profile：声明里设置的字段要覆盖它，没设置的沿用它。
        (cls.root / "hub" / "profiles" / "demo@h1.json").write_text(json.dumps({
            "name": "demo@h1", "slot": "heavy", "locks": ["hub-lock"],
            "timeout": "1h", "artifacts": ["hub/*.log"]}), encoding="utf-8")
        cls.hub = Hub(cls.config)
        for name, slots in (("h1", {"heavy": 1, "light": 2}),
                            ("h2", {"heavy": 1, "light": 1})):
            cls.hub.store.seed_host(name, slots, [])
            cls.hub.store.heartbeat(name, "seed", [])
        cls.server = HubServer(("127.0.0.1", 0), Handler)
        cls.server.hub = cls.hub
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.hub.close()
        cls.hub.store.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        self.case.cleanup()

    def submit(self, repo: Path, *argv):
        code, out, err = run_cli("--config", str(self.config.path),
                                 "--hub", self.url, "--token", TOKEN,
                                 "--repo", str(repo), "--no-base-ref",
                                 "--detach", *argv)
        self.assertEqual(code, 0, out + err)
        job_id = next(line.split()[1] for line in out.splitlines()
                      if line.startswith("queued "))
        return self.hub.store.job(job_id), out

    def test_command_and_task_without_project_or_host(self):
        repo = make_repo(Path(self.case.name),
                         {"project": "demo", "host": "h1", "timeout": "20m",
                          "artifacts": ["reports/*.xml"],
                          "tasks": {"verify": ["python", "-c", "print(1)"]}})
        job, out = self.submit(repo, "--", "python", "-c", "print(2)")
        self.assertEqual((job["project"], job["host"]), ("demo", "h1"))
        self.assertIn("project=demo host=h1", out)
        self.assertEqual(job["cmd"], ["python", "-c", "print(2)"])
        # 声明的 timeout 覆盖 hub profile；没声明的 slot/locks 沿用 hub profile
        self.assertEqual(job["timeout"], 1200)
        self.assertEqual(job["slot"], "heavy")
        self.assertEqual(job["locks"], ["hub-lock"])
        self.assertEqual(job["artifact_patterns"], ["reports/*.xml"])
        payload = self.hub._exec_payload(job)
        self.assertEqual(payload["profile_data"]["artifacts"], ["reports/*.xml"])

        job, out = self.submit(repo, "verify")
        self.assertEqual((job["project"], job["host"]), ("demo", "h1"))
        self.assertEqual(job["cmd"], ["python", "-c", "print(1)"])
        self.assertIn("task=verify", out)

    def test_declared_slot_and_locks_replace_the_hub_profile(self):
        repo = make_repo(Path(self.case.name),
                         {"project": "demo", "host": "h1", "slot": "light",
                          "locks": []})
        job, _ = self.submit(repo, "--", "true")
        self.assertEqual(job["slot"], "light")
        self.assertEqual(job["locks"], [])
        self.assertEqual(job["timeout"], 3600)
        self.assertIsNone(job["artifact_patterns"])
        self.assertEqual(self.hub._exec_payload(job)["profile_data"]["artifacts"],
                         ["hub/*.log"])

    def test_explicit_cli_arguments_override_the_declaration(self):
        repo = make_repo(Path(self.case.name),
                         {"project": "demo", "host": "h1", "slot": "light"})
        job, _ = self.submit(repo, "--project", "other", "--host", "h2",
                             "--slot", "heavy", "--", "true")
        self.assertEqual((job["project"], job["host"], job["slot"]),
                         ("other", "h2", "heavy"))

    def test_without_declaration_the_submission_is_unchanged(self):
        repo = make_repo(Path(self.case.name))
        job, out = self.submit(repo, "--project", "demo", "--host", "h1",
                               "--", "true")
        self.assertEqual((job["project"], job["host"]), ("demo", "h1"))
        self.assertEqual((job["slot"], job["locks"], job["timeout"]),
                         ("heavy", ["hub-lock"], 3600))
        self.assertIsNone(job["artifact_patterns"])
        self.assertNotIn("declaration=", out)

    def test_hub_rejects_escaping_artifact_patterns(self):
        with self.assertRaises(ValueError):
            self.hub.create_job({"project": "demo", "host": "h1",
                                 "cmd": ["true"],
                                 "client_version": "0.3.0",
                                 "artifact_patterns": ["../secret"]})


if __name__ == "__main__":
    unittest.main()
