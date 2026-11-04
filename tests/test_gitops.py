"""快照提交与增量 bundle：不污染工作区/index，bundle 只带增量。"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from calltable import gitops


def git(cwd, *args):
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                         text=True, check=True)
    return out.stdout.strip()


class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "tester")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ 快照
    def test_snapshot_includes_untracked_and_leaves_worktree_alone(self):
        (self.repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("untracked\n", encoding="utf-8")
        (self.repo / "ignored.txt").write_text("nope\n", encoding="utf-8")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")

        status_before = git(self.repo, "status", "--porcelain")
        index_before = git(self.repo, "ls-files", "-s")
        head_before = git(self.repo, "rev-parse", "HEAD")

        sha, has_head = gitops.snapshot_commit(self.repo)
        self.assertTrue(has_head)
        self.assertEqual(len(sha), 40)

        tree = git(self.repo, "ls-tree", "-r", "--name-only", sha).splitlines()
        self.assertIn("new.txt", tree)
        self.assertIn("sub/deep.txt", tree)
        self.assertIn("tracked.txt", tree)
        self.assertNotIn("ignored.txt", tree)
        blob = git(self.repo, "show", f"{sha}:tracked.txt")
        self.assertEqual(blob, "modified")

        # 工作区与 index 完全没变
        self.assertEqual(git(self.repo, "status", "--porcelain"), status_before)
        self.assertEqual(git(self.repo, "ls-files", "-s"), index_before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head_before)
        # 临时 ref 必须已经删掉
        refs = git(self.repo, "for-each-ref", "refs/tb")
        self.assertEqual(refs, "")

    def test_snapshot_on_empty_repository(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        (empty / "a.txt").write_text("a\n", encoding="utf-8")
        sha, has_head = gitops.snapshot_commit(empty)
        self.assertFalse(has_head)
        self.assertEqual(git(empty, "ls-tree", "-r", "--name-only", sha), "a.txt")

    # ------------------------------------------------------------ bundle
    def test_incremental_bundle_has_prerequisite_and_fetches(self):
        """真实的父子提交链：第二次只发增量，前置提交由 bundle 头部声明。"""
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "second")
        second = git(self.repo, "rev-parse", "HEAD")

        full = Path(self.tmp.name) / "full.bundle"
        self.assertTrue(gitops.create_bundle(self.repo, base, None, full))
        self.assertEqual(gitops.bundle_prerequisites(full), [])
        mirror = Path(self.tmp.name) / "mirror.git"
        gitops.ingest_bundle(mirror, full)
        self.assertTrue(gitops.has_commit(mirror, base))
        self.assertFalse(gitops.has_commit(mirror, second))

        incremental = Path(self.tmp.name) / "inc.bundle"
        self.assertTrue(gitops.create_bundle(self.repo, second, base, incremental))
        self.assertEqual(gitops.bundle_prerequisites(incremental), [base])
        gitops.ingest_bundle(mirror, incremental)
        self.assertTrue(gitops.has_commit(mirror, second))

    def test_snapshot_bundle_only_carries_the_snapshot(self):
        """快照提交是 HEAD 的兄弟：增量 bundle 只含快照本身。"""
        head = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "new.txt").write_text("n\n", encoding="utf-8")
        sha, _ = gitops.snapshot_commit(self.repo)
        bundle = Path(self.tmp.name) / "snap.bundle"
        self.assertTrue(gitops.create_bundle(self.repo, sha, head, bundle))
        self.assertEqual(gitops.bundle_prerequisites(bundle), [head])
        mirror = Path(self.tmp.name) / "m2.git"
        gitops.ensure_mirror(mirror)
        base_bundle = Path(self.tmp.name) / "base.bundle"
        gitops.create_bundle(self.repo, head, None, base_bundle)
        gitops.ingest_bundle(mirror, base_bundle)
        gitops.ingest_bundle(mirror, bundle)
        self.assertTrue(gitops.has_commit(mirror, sha))

    def test_no_new_commits_produces_no_bundle(self):
        sha, _ = gitops.snapshot_commit(self.repo)
        self.assertFalse(gitops.create_bundle(self.repo, sha, sha,
                                             Path(self.tmp.name) / "x.bundle"))

    def test_have_commits_batch(self):
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "b.txt").write_text("b\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "b")
        second = git(self.repo, "rev-parse", "HEAD")
        mirror = Path(self.tmp.name) / "hb.git"
        bundle = Path(self.tmp.name) / "hb.bundle"
        gitops.create_bundle(self.repo, second, None, bundle)
        gitops.ingest_bundle(mirror, bundle)
        self.assertEqual(gitops.have_commits(mirror, [base, second, "0" * 40]),
                         [base, second])
        self.assertEqual(gitops.have_commits(mirror, []), [])
        self.assertEqual(gitops.have_commits(mirror, ["not-a-sha"]), [])

    def test_keep_refs_are_pruned(self):
        sha, _ = gitops.snapshot_commit(self.repo)
        mirror = Path(self.tmp.name) / "keep.git"
        bundle = Path(self.tmp.name) / "keep.bundle"
        gitops.create_bundle(self.repo, sha, None, bundle)
        gitops.ingest_bundle(mirror, bundle)
        refs = gitops.keep_refs(mirror)
        self.assertTrue(refs)
        self.assertEqual(gitops.prune_keep_refs(mirror, 0), 0)
        self.assertEqual(gitops.prune_keep_refs(mirror, time.time() + 60), len(refs))
        self.assertEqual(gitops.keep_refs(mirror), [])

    def test_git_runs_with_cross_platform_config(self):
        # A6：-c core.autocrlf=false -c core.longpaths=true 必须真的生效
        self.assertEqual(git(self.repo, "config", "--get", "core.autocrlf"), "false")
        self.assertEqual(git(self.repo, "config", "--get", "core.longpaths"), "true")

    def test_bundle_multi_can_carry_a_base_tip(self):
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "snap.txt").write_text("s\n", encoding="utf-8")
        snap, _ = gitops.snapshot_commit(self.repo)
        # 造一个和 snapshot 分叉的 base，模拟「本地 origin/main 不在快照祖先里」
        base2 = git(self.repo, "commit-tree", "HEAD^{tree}", "-p", base,
                    "-m", "other base")
        out = Path(self.tmp.name) / "multi.bundle"
        self.assertTrue(gitops.create_bundle_multi(
            self.repo, [(snap, "snapshots"), (base2, "bases")], [], out))
        mirror = Path(self.tmp.name) / "multi.git"
        gitops.ingest_bundle(mirror, out)
        self.assertTrue(gitops.has_commit(mirror, snap))
        self.assertTrue(gitops.has_commit(mirror, base2))
        # 基线 tip 也要留 keep ref，hub 之后才能按 sha 现打自足 bundle
        kept = {entry["sha"] for entry in gitops.keep_refs(mirror)}
        self.assertIn(base2, kept)

    def test_clone_shared_keeps_base_refs_isolated(self):
        root = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "one")
        (self.repo / "one.txt").write_text("1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "one")
        base_one = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "head.txt").write_text("h\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "head")
        head = git(self.repo, "rev-parse", "HEAD")

        git(self.repo, "checkout", "-q", "-b", "two", root)
        (self.repo / "two.txt").write_text("2\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "two")
        base_two = git(self.repo, "rev-parse", "HEAD")

        mirror = Path(self.tmp.name) / "iso.git"
        bundle = Path(self.tmp.name) / "iso.bundle"
        gitops.create_bundle_multi(self.repo, [(head, "snapshots"),
                                              (base_two, "bases")], [], bundle)
        gitops.ingest_bundle(mirror, bundle)

        results = {}
        barrier = threading.Barrier(2)

        def worker(name, base):
            barrier.wait()
            work = Path(self.tmp.name) / name
            gitops.clone_shared(mirror, work, head)
            gitops.set_base_ref(work, "origin/main", base)
            results[name] = {
                "ref": git(work, "rev-parse", "origin/main"),
                "merge": git(work, "merge-base", "HEAD", "origin/main"),
            }

        threads = [threading.Thread(target=worker, args=("a", base_one)),
                   threading.Thread(target=worker, args=("b", base_two))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results["a"]["ref"], base_one)
        self.assertEqual(results["a"]["merge"], base_one)
        self.assertEqual(results["b"]["ref"], base_two)
        self.assertEqual(results["b"]["merge"], root)
        # 两次 clone 的 ref 互不影响（不同目录、不同值）
        self.assertNotEqual(results["a"]["ref"], results["b"]["ref"])

    def test_worktree_add_and_remove(self):
        sha, _ = gitops.snapshot_commit(self.repo)
        mirror = Path(self.tmp.name) / "m.git"
        gitops.ensure_mirror(mirror)
        bundle = Path(self.tmp.name) / "b.bundle"
        gitops.create_bundle(self.repo, sha, None, bundle)
        gitops.ingest_bundle(mirror, bundle)
        work = Path(self.tmp.name) / "work"
        gitops.worktree_add(mirror, work, sha)
        self.assertTrue((work / "tracked.txt").exists())
        self.assertEqual(git(work, "rev-parse", "HEAD"), sha)
        gitops.worktree_remove(mirror, work)
        self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()
