"""Store 的连接是整个进程共用的：每条语句都要在 store.lock 里执行并取完结果。

回归背景：janitor 调的 ``Store.checkpoint()`` 曾经不持锁。``PRAGMA wal_checkpoint``
是一条会返回一行结果的写语句，从它返回到游标释放之间一直算「进行中」；这段时间里
请求线程在同一连接上 COMMIT 会报 ``cannot commit transaction - SQL statements in
progress``。平时这个窗口只有几微秒，机器负载很高时 janitor 线程拿到结果后迟迟排
不上，端到端测试里的 ``ct run --detach`` 就撞上过一次。
"""

from __future__ import annotations

import ast
import tempfile
import threading
import unittest
from pathlib import Path

from calltable import db as db_module
from calltable.db import Store


class _HookedConn:
    """包住真实连接：checkpoint 的语句返回之后、游标释放之前，先调一次 hook。

    用它把那个几微秒的窗口撑开，不靠 sleep 和运气。
    """

    def __init__(self, real, hook):
        self._real = real
        self._hook = hook

    def execute(self, sql, *args):
        cursor = self._real.execute(sql, *args)
        if "wal_checkpoint" in sql:
            self._hook()
        return cursor

    def __getattr__(self, name):
        return getattr(self._real, name)


class _KeepCursorsConn:
    """包住真实连接：留住每个游标，模拟游标没有被及时回收的情况。"""

    def __init__(self, real):
        self._real = real
        self.cursors = []

    def execute(self, sql, *args):
        cursor = self._real.execute(sql, *args)
        self.cursors.append(cursor)
        return cursor

    def __getattr__(self, name):
        return getattr(self._real, name)


class CheckpointLockingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "hub.sqlite3"
        self.store = Store(self.db)
        self.store.seed_host("h1", {"light": 2}, [])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _job(self, label: str) -> dict:
        return self.store.create_job(project="p", host="h1", slot="light",
                                     locks=[], cmd=["true"], label=label)

    def test_checkpoint_never_overlaps_another_threads_commit(self):
        outcome: dict = {}

        def submit_if_lock_is_free():
            # 另一个线程（相当于 hub 的请求线程）：此刻拿得到锁就提交一个任务。
            # 拿不到说明 checkpoint 还持着锁，它的语句不会和别人的事务交叠。
            if not self.store.lock.acquire(blocking=False):
                outcome["locked_out"] = True
                return
            try:
                self._job("during-checkpoint")
            except Exception as exc:
                outcome["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                self.store.lock.release()

        def while_statement_in_progress():
            outcome["hook_ran"] = True
            thread = threading.Thread(target=submit_if_lock_is_free)
            thread.start()
            thread.join()

        self.store.conn = _HookedConn(self.store.conn, while_statement_in_progress)
        self._job("before-checkpoint")  # WAL 里有东西，checkpoint 不是空跑
        self.store.checkpoint()

        self.assertTrue(outcome.get("hook_ran"), "checkpoint did not run the PRAGMA")
        self.assertNotIn("error", outcome,
                         "a commit on another thread ran while the checkpoint "
                         "statement was still in progress")
        # checkpoint 返回后连接是干净的：没有悬着的事务，照常能提交、能读回。
        self.assertFalse(self.store.conn.in_transaction)
        job = self._job("after-checkpoint")
        self.assertEqual(self.store.job(job["id"])["label"], "after-checkpoint")

    def test_checkpoint_finishes_its_statement_before_returning(self):
        # 同一个线程紧接着开事务提交：如果 checkpoint 的游标没走完就漏出去了
        # （比如被存进了某个属性），这里的 COMMIT 也会报同样的错。
        self.store.conn = _KeepCursorsConn(self.store.conn)
        self.store.checkpoint()
        job = self._job("right-after")
        self.assertEqual(self.store.job(job["id"])["status"], "queued")


class StoreLockCoverageTests(unittest.TestCase):
    """静态检查：Store 里每一处用到 ``self.conn`` 的代码都在 store.lock 里。

    上面的用例只盯住 checkpoint 这一处；这里把「所有语句都持锁」这条约定固定
    下来，以后新加的方法忘了加锁会直接在这里失败，而不是等到高负载才偶发。
    """

    #: 不需要持锁：构造时连接还没被别的线程看到；close 之后不再使用。
    EXEMPT = {"__init__", "close"}

    @staticmethod
    def _is_self_attr(node: ast.AST, *names: str) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr in names
                and isinstance(node.value, ast.Name) and node.value.id == "self")

    def _scan(self, node: ast.AST, locked: bool, found: list) -> None:
        if isinstance(node, ast.With):
            # Condition(self.lock) 和 self.lock 是同一把锁。
            holds = any(self._is_self_attr(item.context_expr, "lock", "_changed")
                        for item in node.items)
            for child in node.body:
                self._scan(child, locked or holds, found)
            return
        if self._is_self_attr(node, "conn"):
            found.append(("self.conn", node.lineno, locked))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr.endswith("_locked")
              and self._is_self_attr(node.func, node.func.attr)):
            found.append((f"self.{node.func.attr}()", node.lineno, locked))
        for child in ast.iter_child_nodes(node):
            self._scan(child, locked, found)

    def test_every_statement_runs_under_the_store_lock(self):
        source = Path(db_module.__file__).read_text(encoding="utf-8")
        store = next(node for node in ast.parse(source).body
                     if isinstance(node, ast.ClassDef) and node.name == "Store")
        checked = 0
        offenders = []
        for method in store.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            # *_locked 方法由调用方持锁；它们的调用点在下面一并检查。
            caller_holds_lock = method.name.endswith("_locked")
            found: list = []
            for statement in method.body:
                self._scan(statement, False, found)
            checked += len(found)
            if method.name in self.EXEMPT or caller_holds_lock:
                continue
            offenders.extend(f"Store.{method.name} line {line}: {what}"
                             for what, line, locked in found if not locked)
        # 防止检查空转（比如类改了名、连接属性换了名字）。
        self.assertGreater(checked, 50)
        self.assertEqual(
            offenders, [],
            "these touch the shared connection outside `with self.lock`; "
            "take the lock (and fetch results before releasing it), or name "
            "the method *_locked if every caller already holds it")


if __name__ == "__main__":
    unittest.main()
