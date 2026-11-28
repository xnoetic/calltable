"""SQLite 存储与调度核心。

调度是 hub 唯一的权威：任务只有在「主机匹配 + 该主机该 slot 未满 + 所有命名锁空闲」
时才被派发。整个判定与状态迁移在同一个 ``BEGIN IMMEDIATE`` 事务里完成，因此
多个执行器同时 claim 也不会超发。

执行器对账（B1/F1）
-------------------
执行器每次启动生成一个 worker 实例 id，claim 时写进 ``jobs.worker``。心跳上报
本实例 id 和「我正在跑的 job id 列表」。hub 在心跳与周期巡检里回收三类任务：

* ``job.worker`` 与该主机当前心跳的 worker 不一致（执行器重启过）；
* 任务连续两次没出现在心跳的 running 列表里（任务丢了）；
* 主机心跳超过 ``HOST_STALE_SECONDS``（默认 15 分钟）没来。

hub 启动时把遗留的 running 任务标成 ``needs_reconcile``，但不刷新主机
``last_seen``，所以「hub 重启 + 执行器也重启」不会把任务永久卡住。
"""

from __future__ import annotations

import sqlite3
import statistics
import threading
import uuid
from pathlib import Path

from .util import iso, json_dumps, json_loads, new_id, now

#: 主机心跳超时（秒）。15 分钟：网络抖动/短暂挂起不应该误杀任务。
HOST_STALE_SECONDS = 900
#: 连续多少次心跳没有报告某任务就回收。
MISSING_STRIKES = 2
#: 单任务日志硬上限。
DEFAULT_LOG_CAP = 256 * 1024 * 1024
#: 预计耗时：同一 estimate key 最近多少个成功任务参与中位数。
ESTIMATE_WINDOW = 20
#: 预计耗时：样本少于此数时明确显示未知，不做猜测。
ESTIMATE_MIN_SAMPLES = 3
#: 预计耗时的进程内缓存时间（秒）。任务完成时会整体失效。
ESTIMATE_CACHE_SECONDS = 10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    name      TEXT PRIMARY KEY,
    os        TEXT NOT NULL DEFAULT '',
    slots     TEXT NOT NULL DEFAULT '{}',
    labels    TEXT NOT NULL DEFAULT '[]',
    note      TEXT NOT NULL DEFAULT '',
    version   TEXT NOT NULL DEFAULT '',
    last_seen REAL NOT NULL DEFAULT 0,
    worker    TEXT NOT NULL DEFAULT '',
    heartbeat_jobs TEXT NOT NULL DEFAULT '[]',
    system    TEXT NOT NULL DEFAULT '{}',
    draining  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    name     TEXT PRIMARY KEY,
    mirror   TEXT NOT NULL,
    created  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bundles (
    id       TEXT PRIMARY KEY,
    project  TEXT NOT NULL,
    path     TEXT NOT NULL,
    size     INTEGER NOT NULL DEFAULT 0,
    created  REAL NOT NULL,
    job_id   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    project          TEXT NOT NULL,
    host             TEXT NOT NULL,
    slot             TEXT NOT NULL,
    locks            TEXT NOT NULL DEFAULT '[]',
    cmd              TEXT NOT NULL,
    profile          TEXT NOT NULL DEFAULT '',
    submitter        TEXT NOT NULL DEFAULT '',
    agent            TEXT NOT NULL DEFAULT '',
    label            TEXT NOT NULL DEFAULT '',
    sha              TEXT NOT NULL DEFAULT '',
    base_ref         TEXT NOT NULL DEFAULT '',
    base_ref_sha     TEXT NOT NULL DEFAULT '',
    base_sha         TEXT NOT NULL DEFAULT '',
    dirty            INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,
    created          REAL NOT NULL,
    group_name       TEXT NOT NULL DEFAULT '',
    targets          TEXT NOT NULL DEFAULT '[]',
    started          REAL,
    finished         REAL,
    exit_code        INTEGER,
    timeout          INTEGER NOT NULL DEFAULT 10800,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker           TEXT NOT NULL DEFAULT '',
    log_bytes        INTEGER NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT '',
    cleanup_at       REAL,
    cpu_total_sec    REAL NOT NULL DEFAULT 0,
    cpu_peak_cores   REAL NOT NULL DEFAULT 0,
    mem_peak         INTEGER NOT NULL DEFAULT 0,
    bundle_id        TEXT NOT NULL DEFAULT '',
    misses           INTEGER NOT NULL DEFAULT 0,
    needs_reconcile  INTEGER NOT NULL DEFAULT 0,
    purged           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
CREATE INDEX IF NOT EXISTS jobs_host ON jobs(host, status);

CREATE TABLE IF NOT EXISTS logs (
    job_id TEXT NOT NULL,
    start  INTEGER NOT NULL,
    end    INTEGER NOT NULL,
    data   BLOB NOT NULL,
    PRIMARY KEY (job_id, start)
);

CREATE TABLE IF NOT EXISTS samples (
    job_id      TEXT NOT NULL,
    ts          REAL NOT NULL,
    cpu_usec    INTEGER NOT NULL,
    mem_current INTEGER NOT NULL,
    mem_peak    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_job ON samples(job_id, ts);

CREATE TABLE IF NOT EXISTS artifacts (
    job_id TEXT NOT NULL,
    name   TEXT NOT NULL,
    size   INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    stored TEXT NOT NULL,
    PRIMARY KEY (job_id, name)
);

-- 0.3：单调递增的事件日志（SSE 断线续传 / webhook 派发来源）。
CREATE TABLE IF NOT EXISTS events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    job_id TEXT NOT NULL DEFAULT '',
    type   TEXT NOT NULL,
    data   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);

-- 0.3：webhook 投递记录，落库以便 hub 重启后继续投递。
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    event        TEXT NOT NULL,
    url          TEXT NOT NULL,
    msg_id       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    last_status  INTEGER,
    last_error   TEXT NOT NULL DEFAULT '',
    created      REAL NOT NULL,
    updated      REAL NOT NULL,
    UNIQUE(job_id, event, url)
);
CREATE INDEX IF NOT EXISTS webhook_due
    ON webhook_deliveries(status, next_attempt);

-- 0.3：幂等取号（key 在窗口内映射到同一个 job）。
CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idempotency_created ON idempotency(created);

-- Cloud Run provider：每个任务一次 job execution，用来对账和估算月度花费。
CREATE TABLE IF NOT EXISTS cloud_executions (
    job_id     TEXT PRIMARY KEY,
    host       TEXT NOT NULL,
    cloud_job  TEXT NOT NULL DEFAULT '',
    execution  TEXT NOT NULL DEFAULT '',
    vcpu       REAL NOT NULL DEFAULT 0,
    memory_gib REAL NOT NULL DEFAULT 0,
    launched   REAL NOT NULL,
    attached   REAL,
    last_seen  REAL,
    ended      REAL,
    state      TEXT NOT NULL DEFAULT 'launching',
    note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS cloud_executions_launched ON cloud_executions(launched);
"""

#: 旧库升级：(表, 列, 定义)
MIGRATIONS = [
    ("hosts", "worker", "TEXT NOT NULL DEFAULT ''"),
    ("hosts", "heartbeat_jobs", "TEXT NOT NULL DEFAULT '[]'"),
    ("hosts", "system", "TEXT NOT NULL DEFAULT '{}'"),
    ("hosts", "draining", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "cpu_total_sec", "REAL NOT NULL DEFAULT 0"),
    ("jobs", "cpu_peak_cores", "REAL NOT NULL DEFAULT 0"),
    ("jobs", "bundle_id", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "agent", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "base_ref", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "base_ref_sha", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "base_sha", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "group_name", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "targets", "TEXT NOT NULL DEFAULT '[]'"),
    ("jobs", "misses", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "needs_reconcile", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "purged", "INTEGER NOT NULL DEFAULT 0"),
    # 0.3
    ("jobs", "webhooks", "TEXT NOT NULL DEFAULT '[]'"),
    ("jobs", "hold_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "last_hold", "REAL"),
    ("jobs", "idempotency_key", "TEXT NOT NULL DEFAULT ''"),
    # Cloud Run 经 git 中转：hub 推到远端仓库、待任务结束后删除的临时 ref（JSON）。
    ("cloud_executions", "git_refs", "TEXT NOT NULL DEFAULT ''"),
    # 溢出组：claim 前的锁模板（JSON，``{host}`` 未展开）；空串表示没记录。
    # provider 主机拉起失败把任务放回队列时用它还原锁。
    ("jobs", "lock_templates", "TEXT NOT NULL DEFAULT ''"),
    # 任务被放回队列的原因（人看的说明，不算错误）。
    ("jobs", "requeue_note", "TEXT NOT NULL DEFAULT ''"),
    # 仓库声明 .calltable.json 的产物 glob（JSON 数组）；空串表示没声明，
    # 沿用 hub profile 的 artifacts。
    ("jobs", "artifact_patterns", "TEXT NOT NULL DEFAULT ''"),
]

#: provider 预留任务时写进 ``jobs.worker`` 的占位前缀；执行器 attach 后替换。
PENDING_WORKER_PREFIX = "pending:"

#: :meth:`Store.requeue_unattached` 的结果。
REQUEUED = "requeued"
#: 执行器已经 attach（用户代码可能已经开始跑）：任务不动。
ALREADY_ATTACHED = "attached"
#: 不符合放回队列的条件（只有这一台目标、已不在 running、没记录锁模板、
#: 已请求取消……）：调用方按原来的方式处理。
NOT_REQUEUED = "not_requeued"

#: :meth:`Store.finish_from_executor` 的判定结果，HTTP 层原样回给执行器。
#: 写进去了。
FINISH_RECORDED = "recorded"
#: 写进去了，但按取消优先记成了 ``cancelled``。
FINISH_CANCELLED = "cancel_precedence"
#: 任务已是同一个终态：什么都没改（重试、pending 重放走这条）。
FINISH_DUPLICATE = "duplicate"
#: 任务已是别的终态：晚到的旧结果，拒绝覆盖。
FINISH_CONFLICT = "conflict"
#: 任务在另一台主机上（被放回队列、换了执行环境）：拒绝。
FINISH_OTHER_OWNER = "other_owner"
#: 任务现在不在 running（已回到队列）：拒绝。
FINISH_NOT_RUNNING = "not_running"
#: 任务不存在（正好被 janitor 清掉）：HTTP 层按 404 回。
FINISH_UNKNOWN = "unknown"
#: 请求本身不可能成功（body 不是对象、status 不是终态、exit_code 不是整数）：
#: HTTP 层直接判的，不进 :meth:`Store.finish_from_executor`。
FINISH_BAD_REQUEST = "bad_request"

RUNNING = "running"
QUEUED = "queued"
TERMINAL = ("succeeded", "failed", "cancelled", "timeout", "error")
#: 取消的任务没有真实退出码时用的退出码（与排队中直接取消一致）。
CANCELLED_EXIT_CODE = -2


def cancel_precedence(status: str, exit_code: int | None,
                      error: str) -> tuple[str, int | None, str]:
    """已请求取消的任务：任何终态都记为 ``cancelled``（取消优先）。

    取消和领取/准备/对账几乎同时发生时，执行器可能报 ``error`` / ``succeeded``，
    hub 的心跳对账或主机失联回收会写 ``error``（exit -1）——这些都不能让调用方
    误以为是真故障。原来的状态和说明保留在 ``error`` 里；没有真实退出码
    （``None`` 或对账用的 ``-1``）时退出码记 ``-2``。
    """
    if exit_code is None or exit_code == -1:
        exit_code = CANCELLED_EXIT_CODE
    # 执行器自己报的 cancelled（准备阶段中止时没有退出码）也按上面补 -2。
    if status == "cancelled":
        return status, exit_code, error
    note = f"cancel requested; reported {status}"
    if error:
        note = f"{note}: {error}"
    return "cancelled", exit_code, note


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in MIGRATIONS:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


class IdempotencyConflict(ValueError):
    """同一个 idempotency key 被用于不同参数的提交。"""


class Store:
    """每个进程一个连接；hub 是 ThreadingHTTPServer，用一个锁串行化对连接的访问。

    约定：``self.conn`` 上的每条语句（读、写、PRAGMA 都算）都要在 ``self.lock``
    里执行，并且在放锁前把结果取完（``fetchone`` / ``fetchall``）。连接是整个
    进程共用的，事务状态、「进行中的语句」和错误信息都挂在连接上：一个线程没走完
    的写语句会让另一个线程的 COMMIT 报 ``cannot commit transaction - SQL
    statements in progress``，报错文本也可能被别的线程覆盖。名字以 ``_locked``
    结尾的方法由调用方持锁。
    """

    def __init__(self, db_path: str | Path, log_cap: int = DEFAULT_LOG_CAP,
                 estimate_window: int = ESTIMATE_WINDOW,
                 estimate_min_samples: int = ESTIMATE_MIN_SAMPLES):
        self.path = str(db_path)
        self.log_cap = int(log_cap)
        self.estimate_window = max(1, int(estimate_window))
        self.estimate_min_samples = max(1, int(estimate_min_samples))
        # hub 注入：全局 webhook URL / 看板链接 / 幂等窗口。
        self.webhook_urls: list[str] = []
        self.dashboard_base_url = ""
        self.idempotency_window = 86400
        #: provider 注入：主机名 → 冷却截止时间。冷却中的主机在 claim 里按
        #: 「不可用」处理（与 drain 相同），组任务交给其余成员。
        self.host_cooldowns: dict[str, float] = {}
        self.lock = threading.RLock()
        self.conn = connect(db_path)
        # 状态变化通知：长轮询与 SSE 用条件变量等待，不短间隔轮询。
        self._changed = threading.Condition(self.lock)
        self.change_seq = 0
        self._estimate_cache: dict[tuple, tuple[float, dict | None]] = {}

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 变化通知
    def _bump(self) -> None:
        """状态变化：唤醒长轮询 / SSE 线程（调用方已持有 self.lock）。"""
        self.change_seq += 1
        self._changed.notify_all()

    def wait_change(self, seq: int, timeout: float) -> int:
        """等待状态变化；返回当前 change_seq。"""
        with self._changed:
            if self.change_seq != seq:
                return self.change_seq
            self._changed.wait(max(0.0, float(timeout)))
            return self.change_seq

    # ------------------------------------------------------------ 启动
    def mark_running_for_reconcile(self) -> int:
        """hub 重启：遗留的 running 任务等待心跳对账，不刷新主机 last_seen。"""
        with self.lock:
            cursor = self.conn.execute(
                "UPDATE jobs SET needs_reconcile=1 WHERE status='running'")
            return cursor.rowcount

    # ------------------------------------------------------------ host
    def seed_host(self, name: str, slots: dict, labels: list, note: str = "") -> None:
        """只在缺失时插入主机配置；**不**刷新 last_seen（否则会掩盖失联）。"""
        with self.lock:
            # 只同步配置字段；last_seen / worker 保留运行期事实。
            self.conn.execute(
                "INSERT INTO hosts(name, os, slots, labels, note, version, last_seen) "
                "VALUES(?,?,?,?,?,?,0) ON CONFLICT(name) DO UPDATE SET "
                "slots=excluded.slots, labels=excluded.labels, note=excluded.note",
                (name, "", json_dumps(slots), json_dumps(labels), note, ""))

    def heartbeat(self, name: str, worker: str, running: list[str],
                  os_name: str = "", version: str = "",
                  system: dict | None = None, *,
                  ephemeral: bool = False) -> list[dict]:
        """记录一次心跳并立即对账；返回被回收的任务。

        对账规则见模块文档。``running`` 必须是该执行器实例当前持有的 job id。

        ``ephemeral=True``（Cloud Run 这类一次一任务的主机）：同一主机名下会有
        多个执行器实例同时在跑，只对账本实例持有的任务，不因 worker 不同而回收
        别人的任务；丢失的 execution 由 provider 的对账负责。
        """
        reaped: list[dict] = []
        now_ts = now()
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM hosts WHERE name=?", (name,)).fetchone()
                system_json = json_dumps(system) if system else None
                if row is None:
                    self.conn.execute(
                        "INSERT INTO hosts(name, os, slots, labels, note, version, "
                        "last_seen, worker, heartbeat_jobs, system) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (name, os_name, "{}", "[]", "", version, now_ts, worker,
                         json_dumps(sorted(running)), system_json or "{}"))
                else:
                    self.conn.execute(
                        "UPDATE hosts SET last_seen=?, worker=?, heartbeat_jobs=?, "
                        "os=COALESCE(NULLIF(?,''), os), "
                        "version=COALESCE(NULLIF(?,''), version), "
                        "system=CASE WHEN ? IS NULL THEN system ELSE ? END "
                        "WHERE name=?",
                        (now_ts, worker, json_dumps(sorted(running)), os_name,
                         version, system_json, system_json, name))
                reported = set(running)
                jobs = self.conn.execute(
                    "SELECT * FROM jobs WHERE status='running' AND host=?",
                    (name,)).fetchall()
                for job in jobs:
                    if ephemeral and job["worker"] != worker:
                        continue
                    if job["worker"] != worker:
                        # 执行器重启（或这条任务从来没绑定 worker）：本实例不会再
                        # 报告它，立即回收，槽位/锁马上释放。
                        self._finish_locked(
                            job["id"], "error", -1,
                            f"executor worker changed ({job['worker'] or '-'} -> "
                            f"{worker}); reaped")
                        reaped.append(self.job(job["id"]))
                        continue
                    if job["id"] in reported:
                        self.conn.execute(
                            "UPDATE jobs SET misses=0, needs_reconcile=0 WHERE id=?",
                            (job["id"],))
                        if ephemeral:
                            self.conn.execute(
                                "UPDATE cloud_executions SET last_seen=? "
                                "WHERE job_id=?", (now_ts, job["id"]))
                        continue
                    misses = int(job["misses"]) + 1
                    if misses >= MISSING_STRIKES:
                        self._finish_locked(
                            job["id"], "error", -1,
                            "job missing from executor heartbeat "
                            f"{misses} times; reaped")
                        reaped.append(self.job(job["id"]))
                    else:
                        self.conn.execute(
                            "UPDATE jobs SET misses=? WHERE id=?", (misses, job["id"]))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return [job for job in reaped if job]

    def touch_host(self, name: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE hosts SET last_seen=? WHERE name=?", (now(), name))

    def reap_stale_hosts(self, stale_seconds: int = HOST_STALE_SECONDS,
                         skip_hosts: tuple[str, ...] | list[str] = ()) -> list[dict]:
        """主机心跳超时：回收它名下的 running 任务。

        ``skip_hosts``：由 provider 自己按 execution 对账的主机（Cloud Run）。
        """
        reaped: list[dict] = []
        cutoff = now() - stale_seconds
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self.conn.execute(
                    "SELECT h.name AS host, h.last_seen AS seen, j.* "
                    "FROM jobs j JOIN hosts h ON h.name=j.host "
                    "WHERE j.status='running'").fetchall()
                for row in rows:
                    if row["host"] in skip_hosts:
                        continue
                    seen = row["seen"]
                    if seen and seen > cutoff:
                        continue
                    self._finish_locked(
                        row["id"], "error", -1,
                        f"host {row['host']} heartbeat lost "
                        f"(last seen {int(now() - (seen or 0))}s ago); reaped")
                    reaped.append(self.job(row["id"]))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return [job for job in reaped if job]

    def hosts(self) -> list[dict]:
        """主机 + 槽位用量 + 锁 + 正在跑的任务（一次查询，避免 N+1）。"""
        with self.lock:
            host_rows = self.conn.execute("SELECT * FROM hosts ORDER BY name").fetchall()
            job_rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status='running' ORDER BY started").fetchall()
        by_host: dict[str, list[dict]] = {}
        for job in job_rows:
            by_host.setdefault(job["host"], []).append(self._job_row(job))
        result = []
        now_ts = now()
        for row in host_rows:
            running = by_host.get(row["name"], [])
            used: dict[str, int] = {}
            locks: list[str] = []
            for job in running:
                used[job["slot"]] = used.get(job["slot"], 0) + 1
                locks.extend(job["locks"])
            system = json_loads(row["system"], {}) or {}
            result.append({
                "name": row["name"],
                "os": row["os"],
                "slots": json_loads(row["slots"], {}),
                "labels": json_loads(row["labels"], []),
                "note": row["note"],
                "version": row["version"],
                "last_seen": row["last_seen"],
                "online": (now_ts - row["last_seen"]) < 90,
                "worker": row["worker"],
                "used": used,
                "locks": sorted(set(locks)),
                "running": running,
                "system": system,
                "disk_low": bool(system.get("disk_low")),
                "draining": bool(row["draining"]),
            })
        return result

    def host(self, name: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM hosts WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return {"name": row["name"], "slots": json_loads(row["slots"], {}),
                "worker": row["worker"], "last_seen": row["last_seen"],
                "system": json_loads(row["system"], {}) or {},
                "disk_low": bool((json_loads(row["system"], {}) or {}).get("disk_low")),
                "draining": bool(row["draining"])}

    def set_draining(self, name: str, draining: bool) -> dict | None:
        """打开/关闭 drain：打开后调度不再给这台主机派新任务。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM hosts WHERE name=?", (name,)).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE hosts SET draining=? WHERE name=?",
                              (1 if draining else 0, name))
        return self.host(name)

    # ------------------------------------------------------------ project
    def ensure_project(self, name: str, mirror: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO projects(name, mirror, created) VALUES(?,?,?) "
                "ON CONFLICT(name) DO NOTHING", (name, mirror, now()))

    def project(self, name: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ bundle
    def add_bundle(self, bundle_id: str, project: str, path: str, size: int) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO bundles(id, project, path, size, created) "
                "VALUES(?,?,?,?,?)", (bundle_id, project, path, size, now()))

    def bundle(self, bundle_id: str) -> dict | None:
        if not bundle_id:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM bundles WHERE id=?", (bundle_id,)).fetchone()
        return dict(row) if row else None

    def attach_bundle(self, bundle_id: str, job_id: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE bundles SET job_id=? WHERE id=?",
                              (job_id, bundle_id))

    # ------------------------------------------------------------ jobs
    def create_job(self, *, project: str, host: str, slot: str, locks: list[str],
                   cmd: list[str], profile: str = "", submitter: str = "",
                   agent: str = "", label: str = "", sha: str = "",
                   base_ref: str = "", base_ref_sha: str = "",
                   base_sha: str = "", dirty: bool = False,
                   timeout: int = 10800, bundle_id: str = "",
                   group_name: str = "", targets: list[str] | None = None,
                   webhooks: list[str] | None = None,
                   hold_seconds: int = 0,
                   idempotency: dict | None = None,
                   artifact_patterns: list[str] | None = None) -> dict:
        """创建任务；``idempotency`` = {key, fingerprint, window} 时原子去重。"""
        job_id = new_id()
        replay_id = ""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if idempotency:
                    replay_id = self._idempotent_job_locked(idempotency)
                    if replay_id:
                        self.conn.execute("COMMIT")
                        job = self.job(replay_id)
                        if job is not None:
                            job["idempotent_replay"] = True
                            return job
                        replay_id = ""
                self.conn.execute(
                    """INSERT INTO jobs(id, project, host, slot, locks, cmd, profile,
                           submitter, agent, label, sha, base_ref, base_ref_sha,
                           base_sha, dirty, status, created, timeout, bundle_id,
                           group_name, targets, webhooks, hold_seconds,
                           last_hold, idempotency_key, artifact_patterns)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, project, host, slot, json_dumps(locks), json_dumps(cmd),
                     profile, submitter, agent, label, sha, base_ref, base_ref_sha,
                     base_sha, 1 if dirty else 0, QUEUED, now(), timeout, bundle_id,
                     group_name, json_dumps(targets or [host]),
                     json_dumps(webhooks or []), int(hold_seconds or 0),
                     (now() if hold_seconds else None),
                     (idempotency or {}).get("key", ""),
                     ("" if artifact_patterns is None
                      else json_dumps(list(artifact_patterns)))))
                if bundle_id:
                    self.conn.execute("UPDATE bundles SET job_id=? WHERE id=?",
                                      (job_id, bundle_id))
                if idempotency:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO idempotency(key, job_id, "
                        "fingerprint, created) VALUES(?,?,?,?)",
                        (idempotency["key"], job_id, idempotency["fingerprint"],
                         now()))
                self._emit_locked(job_id, "job.queued")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.job(job_id)

    def _idempotent_job_locked(self, idempotency: dict) -> str:
        """已有 key：一致则返回旧 job id，不一致抛 IdempotencyConflict。

        过期的 key 直接删掉，按新提交处理。
        """
        key = idempotency["key"]
        row = self.conn.execute(
            "SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return ""
        window = float(idempotency.get("window") or 86400)
        if now() - float(row["created"]) > window:
            self.conn.execute("DELETE FROM idempotency WHERE key=?", (key,))
            return ""
        existing = self.job(row["job_id"])
        if existing is None:  # 任务行已被清掉：允许重用 key
            self.conn.execute("DELETE FROM idempotency WHERE key=?", (key,))
            return ""
        if row["fingerprint"] != idempotency["fingerprint"]:
            raise IdempotencyConflict(
                f"idempotency key {key!r} was already used with different "
                f"submission parameters (existing job {row['job_id']}); use a "
                "new key or resubmit with the same parameters")
        return row["job_id"]

    def job(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_row(row) if row else None

    def job_bundle(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT b.* FROM bundles b JOIN jobs j ON j.bundle_id=b.id "
                "WHERE j.id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def _job_row(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        data["locks"] = json_loads(data["locks"], [])
        data["cmd"] = json_loads(data["cmd"], [])
        data["targets"] = json_loads(data.get("targets"), [])
        data["webhooks"] = json_loads(data.get("webhooks"), []) or []
        data["hold_seconds"] = int(data.get("hold_seconds") or 0)
        data["locks_display"] = self._display_locks(
            data["locks"], data["targets"], data["host"], data["status"])
        data["dirty"] = bool(data["dirty"])
        data["cancel_requested"] = bool(data["cancel_requested"])
        data["needs_reconcile"] = bool(data.get("needs_reconcile"))
        data["purged"] = bool(data.get("purged"))
        data["requeue_note"] = data.get("requeue_note") or ""
        patterns = data.get("artifact_patterns") or ""
        data["artifact_patterns"] = json_loads(patterns, None) if patterns else None
        data.pop("bundle_id", None)
        data.pop("misses", None)
        data.pop("lock_templates", None)
        return data

    @staticmethod
    def _display_locks(locks: list[str], targets: list[str], host: str,
                       status: str) -> list[str]:
        """把 ``{host}`` 模板按展示需要展开。

        * 运行中的任务：展开成实际主机；
        * 排队中的组任务：展开成 ``{runner-a|runner-b}``；
        * 排队中的单主机任务：展开成该主机。
        """
        shown: list[str] = []
        for lock in locks:
            if "{host}" in lock:
                if status == RUNNING or not targets:
                    replacement = host
                else:
                    replacement = "{" + "|".join(targets) + "}"
                shown.append(lock.replace("{host}", replacement))
            else:
                shown.append(lock)
        return shown

    def jobs(self, status: str | None = None, limit: int = 100,
             project: str | None = None) -> list[dict]:
        sql = "SELECT * FROM jobs"
        where, args = [], []
        if status == "running":
            where.append("status='running'")
        elif status == "queued":
            where.append("status='queued'")
        elif status:
            where.append("status=?")
            args.append(status)
        if project:
            where.append("project=?")
            args.append(project)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self._job_row(r) for r in rows]

    # ------------------------------------------------------------ 队列可见性
    def _lock_key_set(self, locks: list[str], host: str) -> set[str]:
        return self._lock_keys(self.expand_locks(locks, host), host)

    def _job_targets(self, job: dict) -> list[str]:
        targets = job.get("targets") or []
        return list(targets) or [job["host"]]

    def _competes(self, earlier: dict, job: dict) -> bool:
        """两个排队任务是否在抢同一个 slot 或同一把锁。"""
        first, second = self._job_targets(earlier), self._job_targets(job)
        shared = set(first) & set(second)
        if not shared:
            return False
        if earlier["slot"] == job["slot"]:
            return True
        earlier_keys: set[str] = set()
        for host in first:
            earlier_keys |= self._lock_key_set(earlier["locks"], host)
        job_keys: set[str] = set()
        for host in second:
            job_keys |= self._lock_key_set(job["locks"], host)
        return bool(earlier_keys & job_keys)

    def queue_info(self) -> dict[str, dict]:
        """每个排队任务：已等秒数 + 前面还有几个竞争同一 slot/锁的任务。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status='queued' "
                "ORDER BY created, rowid").fetchall()
        queued = [self._job_row(row) for row in rows]
        now_ts = now()
        info: dict[str, dict] = {}
        for index, job in enumerate(queued):
            ahead_ids = [earlier["id"] for earlier in queued[:index]
                         if self._competes(earlier, job)]
            info[job["id"]] = {
                "queued_seconds": max(0, int(now_ts - job["created"])),
                "ahead": len(ahead_ids),
                "ahead_ids": ahead_ids,
            }
        return info

    # ------------------------------------------------------------ 预计时间
    def _estimate_key(self, job: dict) -> tuple[str, str, str]:
        """(profile, project, slot)：同一 profile 优先，否则按项目。"""
        return (str(job.get("profile") or ""), str(job.get("project") or ""),
                str(job.get("slot") or ""))

    def duration_estimate(self, job: dict) -> dict | None:
        """同一 estimate key 最近 N 次成功任务耗时的中位数。

        样本不足 ``estimate_min_samples`` 时返回 None（客户端必须显示未知）。
        """
        profile, project, slot = self._estimate_key(job)
        key = (profile, project, slot)
        now_ts = now()
        cached = self._estimate_cache.get(key)
        if cached is not None and now_ts - cached[0] < ESTIMATE_CACHE_SECONDS:
            return cached[1]
        with self.lock:
            if profile:
                rows = self.conn.execute(
                    "SELECT started, finished FROM jobs WHERE status='succeeded' "
                    "AND slot=? AND profile=? AND started IS NOT NULL "
                    "AND finished IS NOT NULL ORDER BY finished DESC, rowid DESC LIMIT ?",
                    (slot, profile, self.estimate_window)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT started, finished FROM jobs WHERE status='succeeded' "
                    "AND slot=? AND project=? AND (profile='' OR profile IS NULL) "
                    "AND started IS NOT NULL AND finished IS NOT NULL "
                    "ORDER BY finished DESC, rowid DESC LIMIT ?",
                    (slot, project, self.estimate_window)).fetchall()
        durations = [float(row["finished"]) - float(row["started"])
                     for row in rows]
        result = None
        if len(durations) >= self.estimate_min_samples:
            result = {
                "seconds": max(0, int(round(statistics.median(durations)))),
                "samples": len(durations),
                "window": self.estimate_window,
                "basis": f"{(profile or project)} / {slot}",
            }
        self._estimate_cache[key] = (now_ts, result)
        return result

    def queue_estimates(self, queued: list[dict] | None = None,
                        running: list[dict] | None = None) -> dict[str, dict]:
        """每个排队任务的预计耗时 / 预计等待时间（见 spec §3）。

        等待时间 = 所有阻塞者贡献之和：

        * 排在前面的竞争任务 -> 它自己的预计耗时；
        * 正在跑且竞争的任务 -> max(0, 预计耗时 - 已跑时间)。

        任一阻塞者样本不足就整体未知（``None``），不猜。
        """
        now_ts = now()
        if queued is None:
            with self.lock:
                rows = self.conn.execute(
                    "SELECT * FROM jobs WHERE status='queued' "
                    "ORDER BY created, rowid").fetchall()
            queued = [self._job_row(row) for row in rows]
        if running is None:
            with self.lock:
                rows = self.conn.execute(
                    "SELECT * FROM jobs WHERE status='running' "
                    "ORDER BY started").fetchall()
            running = [self._job_row(row) for row in rows]
        result: dict[str, dict] = {}
        for index, job in enumerate(queued):
            own = self.duration_estimate(job)
            blockers = 0
            unknown = 0
            for earlier in queued[:index]:
                if not self._competes(earlier, job):
                    continue
                estimate = self.duration_estimate(earlier)
                if estimate is None:
                    unknown += 1
                else:
                    blockers += int(estimate["seconds"])
            for active in running:
                if not self._competes(active, job):
                    continue
                estimate = self.duration_estimate(active)
                if estimate is None:
                    unknown += 1
                    continue
                elapsed = max(0.0, now_ts - float(active.get("started") or now_ts))
                blockers += max(0, int(estimate["seconds"] - elapsed))
            result[job["id"]] = {
                "runtime_seconds": (own["seconds"] if own else None),
                "runtime_samples": (own["samples"] if own else 0),
                "runtime_basis": (own["basis"] if own else
                                  f"{job.get('profile') or job.get('project')} "
                                  f"/ {job.get('slot')}"),
                "wait_seconds": (None if unknown else blockers),
                "wait_unknown_blockers": unknown,
                "known": bool(own) and unknown == 0,
            }
        return result

    # ------------------------------------------------------------ 调度
    # ------------------------------------------------------------ 锁语义
    @staticmethod
    def expand_locks(locks: list[str], host: str) -> list[str]:
        """把 ``fullgate@{host}`` 展开成 ``fullgate@runner-a``。"""
        return [lock.replace("{host}", host) for lock in locks]

    @staticmethod
    def _lock_keys(locks: list[str], host: str) -> set[str]:
        """把锁解析成冲突键。

        * ``name``            全局锁；
        * ``name@somehost``   只在该主机上生效（每台主机各一把）。
        """
        keys: set[str] = set()
        for lock in locks:
            if "@" in lock:
                name, _, scope = lock.rpartition("@")
                if scope == host:
                    keys.add(f"{name}@{host}")
            else:
                keys.add(lock)
        return keys

    def _held_lock_keys(self, running: list[sqlite3.Row]) -> set[str]:
        keys: set[str] = set()
        for job in running:
            keys.update(self._lock_keys(json_loads(job["locks"], []), job["host"]))
        return keys

    def claim(self, host: str, worker: str = "") -> dict | None:
        """原子地为 ``host`` 挑一条可运行任务并置为 running。

        * 任务可以写主机名，也可以写主机组（``targets`` 是创建时展开的成员，
          顺序即优先级）。同一任务只会被一台主机领走：首个「在线 + slot 有空 +
          锁空闲」的成员才有资格领取，其余成员继续等。
        * 主机的 ``slots`` 为空（或没配）时上限按 **0** 处理。
        """
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                hosts = {row["name"]: row for row in self.conn.execute(
                    "SELECT * FROM hosts")}
                if host not in hosts:
                    self.conn.execute("COMMIT")
                    return None
                if hosts[host]["draining"]:
                    # drain 中的主机不接新任务（部署重启前用）。
                    self.conn.execute("COMMIT")
                    return None
                if (json_loads(hosts[host]["system"], {}) or {}).get("disk_low"):
                    # 磁盘/ runs 超水位的主机不接新任务，先把空间清出来。
                    self.conn.execute("COMMIT")
                    return None
                now_ts = now()
                if self._cooling_down(host, now_ts):
                    self.conn.execute("COMMIT")
                    return None
                running = self.conn.execute(
                    "SELECT * FROM jobs WHERE status='running'").fetchall()
                used: dict[str, dict[str, int]] = {}
                for job in running:
                    bucket = used.setdefault(job["host"], {})
                    bucket[job["slot"]] = bucket.get(job["slot"], 0) + 1
                held_locks = self._held_lock_keys(running)

                candidates = self.conn.execute(
                    "SELECT * FROM jobs WHERE status='queued' "
                    "ORDER BY created").fetchall()
                chosen = None
                selected_host = None
                for job in candidates:
                    targets = json_loads(job["targets"], []) or [job["host"]]
                    if host not in targets:
                        continue
                    selected = None
                    for member in targets:
                        row = hosts.get(member)
                        if row is None:
                            continue
                        # 离线主机不能作为「首选成员」，否则会把任务饿死。
                        if not row["last_seen"] or now_ts - row["last_seen"] > 90:
                            continue
                        if row["draining"]:
                            continue
                        if (json_loads(row["system"], {}) or {}).get("disk_low"):
                            continue
                        # 冷却中的 provider 主机不能当「首选成员」，否则它会
                        # 挡住排在后面、已经有空位的成员。
                        if self._cooling_down(member, now_ts):
                            continue
                        limits = json_loads(row["slots"], {})
                        limit = int(limits.get(job["slot"], 0))
                        if used.get(member, {}).get(job["slot"], 0) >= limit:
                            continue
                        expanded = self.expand_locks(json_loads(job["locks"], []),
                                                     member)
                        if self._lock_keys(expanded, member) & held_locks:
                            continue
                        selected = member
                        break
                    if selected != host:
                        continue
                    chosen = job
                    selected_host = selected
                    break
                if chosen is None:
                    self.conn.execute("COMMIT")
                    return None
                locks = self.expand_locks(json_loads(chosen["locks"], []),
                                          selected_host)
                # lock_templates 留住展开前的锁：provider 主机没拉起来时
                # （requeue_unattached）要原样放回队列，展开是不可逆的。
                self.conn.execute(
                    "UPDATE jobs SET status='running', started=?, worker=?, "
                    "host=?, locks=?, lock_templates=?, misses=0, "
                    "needs_reconcile=0 WHERE id=? AND status='queued'",
                    (now(), worker, selected_host, json_dumps(locks),
                     chosen["locks"], chosen["id"]))
                self._emit_locked(chosen["id"], "job.started")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            return self.job(chosen["id"])

    def request_cancel(self, job_id: str) -> dict | None:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                job = self.conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job is None:
                    self.conn.execute("COMMIT")
                    return None
                if job["status"] == QUEUED:
                    self.conn.execute(
                        "UPDATE jobs SET status='cancelled', cancel_requested=1, "
                        "finished=?, exit_code=-2 WHERE id=?", (now(), job_id))
                    self._emit_locked(job_id, "job.finished")
                elif job["status"] == RUNNING:
                    self.conn.execute(
                        "UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.job(job_id)

    # ------------------------------------------------------------ Cloud Run
    def _cooling_down(self, host: str, now_ts: float) -> bool:
        return float(self.host_cooldowns.get(host) or 0.0) > now_ts

    def set_host_cooldown(self, host: str, until: float) -> None:
        """provider 主机冷却到 ``until``（0 表示取消）。只在内存里，hub 重启即清空。"""
        with self.lock:
            if until > 0:
                self.host_cooldowns[host] = float(until)
            else:
                self.host_cooldowns.pop(host, None)

    def requeue_unattached(self, job_id: str, host: str, *,
                           note: str) -> tuple[str, dict | None]:
        """provider 主机拉起失败（还没有执行器 attach）：把组任务放回队列。

        条件检查与写入在同一个事务里（与 :meth:`finish_if_status` 同理）：

        * 任务仍是 ``running``、在 ``host`` 上，且 ``targets`` 里除了 ``host``
          还有别的主机、claim 时记下了锁模板、没有请求取消——否则返回
          ``NOT_REQUEUED``，调用方按原来的方式处理（记 error）；
        * worker 已经不是 ``pending:`` 占位（执行器 attach 了）：返回
          ``ALREADY_ATTACHED``，任务不动。

        放回队列时：``targets`` 去掉 ``host``（不会再弹回这台主机），
        ``host`` / ``worker`` / ``started`` / 对账标记 / 锁还原成 claim 之前的样子，
        ``created`` 不变（排队位置不变），原因追加到 ``requeue_note``；发一条
        ``job.requeued`` 事件。
        """
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                outcome = self._requeue_locked(job_id, host, note)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        job = self.job(job_id) if outcome == REQUEUED else None
        return outcome, job

    def _requeue_locked(self, job_id: str, host: str, note: str) -> str:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] != RUNNING or row["host"] != host:
            return NOT_REQUEUED
        targets = json_loads(row["targets"], []) or [row["host"]]
        remaining = [member for member in targets if member != host]
        # 只有这一台目标的任务保持原样（记 error），不看它有没有 attach。
        if not remaining:
            return NOT_REQUEUED
        if not (row["worker"] or "").startswith(PENDING_WORKER_PREFIX):
            return ALREADY_ATTACHED
        if not row["lock_templates"] or row["cancel_requested"]:
            return NOT_REQUEUED
        existing = row["requeue_note"] or ""
        merged = (existing + "; " + note) if existing else note
        # 号码绑定（hold_seconds）按重新入队的时间重新起算：预留期间客户端
        # 不发排队心跳，旧的 last_hold 会让 reap_queue_holds 立刻把它取消。
        self.conn.execute(
            "UPDATE jobs SET status='queued', host=?, worker='', started=NULL, "
            "locks=?, lock_templates='', targets=?, misses=0, needs_reconcile=0, "
            "requeue_note=?, last_hold=CASE WHEN hold_seconds>0 THEN ? "
            "ELSE last_hold END WHERE id=?",
            (row["group_name"] or remaining[0], row["lock_templates"],
             json_dumps(remaining), merged[-2000:], now(), job_id))
        self._emit_locked(job_id, "job.requeued",
                          webhook_urls=self._retract_started_webhooks_locked(job_id))
        return REQUEUED

    def _retract_started_webhooks_locked(self, job_id: str) -> list[str]:
        """放回队列时撤掉 ``job.started`` 投递；返回已经发出过的 URL。

        调用方负责事务；``self.lock`` 必须已持有。

        还没尝试过的直接删掉（接收端从没见过「开始」）；已经发出或正在
        发的也删掉，让真正开跑时的 ``job.started`` 能再投一次（每个事件每个
        URL 只有一条记录），并给这些 URL 补一条 ``job.requeued``。
        """
        rows = self.conn.execute(
            "SELECT url, status, attempts FROM webhook_deliveries "
            "WHERE job_id=? AND event='job.started'", (job_id,)).fetchall()
        sent = [row["url"] for row in rows
                if row["status"] != "pending" or int(row["attempts"] or 0) > 0]
        self.conn.execute(
            "DELETE FROM webhook_deliveries WHERE job_id=? "
            "AND event IN ('job.started', 'job.requeued')", (job_id,))
        return sent

    def attach_job(self, job_id: str, host: str, worker: str) -> dict | None:
        """一次性执行器（``exec --once --job-id``）认领 provider 预留的任务。

        任务必须已被 provider 置为 running、属于 ``host``，且还没有别的执行器
        实例认领（worker 仍是预留占位）；同一实例重复 attach 视为幂等。
        """
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if (row is None or row["status"] != RUNNING
                        or row["host"] != host):
                    self.conn.execute("COMMIT")
                    return None
                current = row["worker"] or ""
                if current != worker and not current.startswith(PENDING_WORKER_PREFIX):
                    self.conn.execute("COMMIT")
                    return None
                ts = now()
                self.conn.execute(
                    "UPDATE jobs SET worker=?, misses=0, needs_reconcile=0 "
                    "WHERE id=?", (worker, job_id))
                self.conn.execute(
                    "UPDATE cloud_executions SET attached=COALESCE(attached, ?), "
                    "last_seen=?, state=CASE WHEN state IN ('launching','launched') "
                    "THEN 'attached' ELSE state END WHERE job_id=?",
                    (ts, ts, job_id))
                self.conn.execute("UPDATE hosts SET last_seen=? WHERE name=?",
                                  (ts, host))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.job(job_id)

    def cloud_record_launch(self, job_id: str, host: str, *, cloud_job: str,
                            vcpu: float, memory_gib: float,
                            launched: float | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cloud_executions(job_id, host, cloud_job, "
                "vcpu, memory_gib, launched, state) VALUES(?,?,?,?,?,?,'launching')",
                (job_id, host, cloud_job, float(vcpu), float(memory_gib),
                 launched if launched is not None else now()))

    def cloud_update(self, job_id: str, **fields) -> None:
        allowed = {"execution", "state", "ended", "note", "attached", "last_seen",
                   "git_refs"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        with self.lock:
            self.conn.execute(f"UPDATE cloud_executions SET {cols} WHERE job_id=?",
                              (*sets.values(), job_id))

    def cloud_executions(self, *, active_only: bool = False,
                         since: float | None = None) -> list[dict]:
        query = "SELECT * FROM cloud_executions"
        clauses, params = [], []
        if active_only:
            clauses.append("ended IS NULL")
        if since is not None:
            clauses.append("(launched >= ? OR ended IS NULL OR ended >= ?)")
            params.extend([since, since])
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY launched"
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def cloud_git_pending(self, host: str) -> list[dict]:
        """已关闭、但推到远端仓库的临时 ref 还没删掉的 execution 记录。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM cloud_executions WHERE host=? AND ended IS NOT NULL "
                "AND git_refs != '' ORDER BY launched", (host,)).fetchall()
        return [dict(row) for row in rows]

    def touch_hold(self, job_id: str) -> dict | None:
        """排队期间的号码绑定心跳；返回更新后的任务（没有任务返回 None）。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT status, hold_seconds FROM jobs WHERE id=?",
                (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == QUEUED and int(row["hold_seconds"] or 0) > 0:
                self.conn.execute("UPDATE jobs SET last_hold=? WHERE id=?",
                                  (now(), job_id))
        return self.job(job_id)

    def reap_queue_holds(self, now_ts: float | None = None) -> list[dict]:
        """排队超时没心跳的 hold 任务自动取消（不影响 running）。"""
        now_ts = now_ts or now()
        reaped: list[dict] = []
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self.conn.execute(
                    "SELECT * FROM jobs WHERE status='queued' AND "
                    "hold_seconds>0 AND "
                    "COALESCE(last_hold, created) + hold_seconds <= ?",
                    (now_ts,)).fetchall()
                for row in rows:
                    last = float(row["last_hold"] or row["created"])
                    self._finish_locked(
                        row["id"], "cancelled", -2,
                        "queue hold expired: no heartbeat for "
                        f"{int(now_ts - last)}s (hold {int(row['hold_seconds'])}s); "
                        "cancelled to free the queue")
                    reaped.append(row["id"])
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return [self.job(job_id) for job_id in reaped]

    def finish(self, job_id: str, *, status: str, exit_code: int | None,
               error: str = "", cleanup_at: float | None = None,
               failure_cleanup_at: float | None = None) -> dict | None:
        """无条件写终态。``failure_cleanup_at``：最终状态不是 ``succeeded``
        （例如取消优先改写成了 ``cancelled``）时改用这个清理时间。

        这是内部原语（hub 自己的对账、测试）：执行器通过 HTTP 上报终态的入口是
        :meth:`finish_from_executor`，那里先判定归属与幂等，不覆盖已交付的结果。
        """
        with self.lock:
            self._finish_locked(job_id, status, exit_code, error, cleanup_at,
                                failure_cleanup_at)
        return self.job(job_id)

    def finish_if_status(self, job_id: str, expected: tuple[str, ...], *,
                         status: str, exit_code: int | None, error: str = "",
                         cleanup_at: float | None = None) -> dict | None:
        """只有任务当前状态仍在 ``expected`` 里才写终态（检查与更新在同一把锁里）。

        给 provider 对账用：它读到的任务快照可能在慢速云 API 调用期间过期，
        执行器在此期间上报的终态不能被覆盖。没写（任务不存在或状态已变）
        返回 None。普通 :meth:`finish` 的语义不变。
        """
        with self.lock:
            row = self.conn.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in expected:
                return None
            self._finish_locked(job_id, status, exit_code, error, cleanup_at)
        return self.job(job_id)

    def finish_from_executor(self, job_id: str, *, status: str,
                             exit_code: int | None, error: str = "",
                             host: str = "", worker: str = "",
                             cleanup_at: float | None = None,
                             failure_cleanup_at: float | None = None
                             ) -> tuple[str, dict | None]:
        """执行器上报终态：先判定归属与幂等，再决定写不写。

        返回 ``(outcome, job)``，outcome 是 :data:`FINISH_RECORDED` 等常量。
        判定与写入在同一把锁里（与 :meth:`finish_if_status` 同理）：晚到的
        结果不能覆盖已经交付给 agent / webhook 的终态，也不能把被重新排队的
        组任务改写完成。拒收一律不写：``status`` / ``exit_code`` /
        ``error`` / ``finished`` / 清理时间都保持原样。

        * 已是终态：一个字都不改。与本条上报（先按取消优先折算）结果相同是
          :data:`FINISH_DUPLICATE`，不同是 :data:`FINISH_CONFLICT`。只比
          status/exit_code：执行器可能给同一条终态补说明，那不是冲突。
        * 不在 ``running``（provider 用 :meth:`requeue_unattached` 放回队列）：
          :data:`FINISH_NOT_RUNNING`。
        * ``host`` 给了且不等于任务现在的主机；或 ``worker`` 给了且不等于
          记录的 claim worker（预留占位 ``pending:`` 不算，那时还没 attach）：
          :data:`FINISH_OTHER_OWNER`。两个身份都是可选的。
        * 其余照 :meth:`finish` 写终态（含取消优先），写成功返回
          :data:`FINISH_RECORDED`，被改写成 ``cancelled`` 返回
          :data:`FINISH_CANCELLED`。

        身份字段是 0.3 客户端才发的：只有携带**原** claim worker 的客户端才
        会被拦在归属检查上，老客户端（只发 status / exit_code / error）不做
        归属判定，行为与 :meth:`finish` 一致——升级 hub 不能把还在跑旧版的
        机群卡住。但老客户端只能得到幂等保护（不能覆盖已经终结的结果），
        没有归属保护：重派之后它报的结果照样会落到新执行那一次的记录上。
        执行器重启后由新实例替旧实例收尾时，要发旧实例的 worker（``.running``
        里记下的那个），不能拿新实例的 id 冒充。
        """
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT status, host, worker, exit_code, cancel_requested "
                    "FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    self.conn.execute("COMMIT")
                    return FINISH_UNKNOWN, None
                current = row["status"]
                if row["cancel_requested"]:
                    # 库里的终态早就是按取消优先写的，比之前先折算，重放才算
                    # 同一个结果。
                    wanted, wanted_exit, _ = cancel_precedence(
                        status, exit_code, error)
                else:
                    wanted, wanted_exit = status, exit_code
                claimed = row["worker"] or ""
                if current in TERMINAL:
                    same = (current == wanted
                            and row["exit_code"] == wanted_exit)
                    outcome = FINISH_DUPLICATE if same else FINISH_CONFLICT
                elif current != RUNNING:
                    outcome = FINISH_NOT_RUNNING
                elif host and row["host"] != host:
                    outcome = FINISH_OTHER_OWNER
                elif (worker and claimed and not claimed.startswith(
                        PENDING_WORKER_PREFIX) and claimed != worker):
                    outcome = FINISH_OTHER_OWNER
                else:
                    self._finish_locked(job_id, status, exit_code, error,
                                        cleanup_at, failure_cleanup_at)
                    outcome = (FINISH_CANCELLED
                               if (wanted, wanted_exit) != (status, exit_code)
                               else FINISH_RECORDED)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return outcome, self.job(job_id)

    def _finish_locked(self, job_id: str, status: str, exit_code: int | None,
                       error: str = "", cleanup_at: float | None = None,
                       failure_cleanup_at: float | None = None) -> None:
        previous = self.conn.execute(
            "SELECT status, cancel_requested FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        if previous is not None and previous["cancel_requested"]:
            status, exit_code, error = cancel_precedence(status, exit_code, error)
        if status != "succeeded" and failure_cleanup_at is not None:
            cleanup_at = failure_cleanup_at
        self.conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, finished=?, error=?, "
            "cleanup_at=COALESCE(?, cleanup_at), needs_reconcile=0 WHERE id=?",
            (status, exit_code, now(), error, cleanup_at, job_id))
        # 已终结的任务重复上报（执行器重放）不再发第二次事件。
        if previous is not None and previous["status"] not in TERMINAL:
            self._emit_locked(job_id, "job.finished")
            self._estimate_cache.clear()

    def append_error(self, job_id: str, note: str) -> None:
        """给任务追加一条说明，不改变 status / exit_code。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT error FROM jobs WHERE id=?", (job_id,)).fetchone()
            existing = (row["error"] if row else "") or ""
            merged = (existing + "; " + note) if existing else note
            self.conn.execute("UPDATE jobs SET error=? WHERE id=?", (merged, job_id))

    # ------------------------------------------------------------ 事件 / webhook
    def _job_snapshot_locked(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?",
                                (job_id,)).fetchone()
        return self._job_row(row) if row is not None else None

    def _emit_locked(self, job_id: str, event_type: str, *,
                     webhook_urls: list[str] | None = None) -> int | None:
        """写一条事件（并给 job.started/finished 排 webhook 投递）。

        ``webhook_urls``：其余事件类型也投递，只投给这些 URL（``job.requeued``
        只发给已经收到过 ``job.started`` 的接收端）。

        调用方负责事务；``self.lock`` 必须已持有。返回事件序号。
        """
        job = self._job_snapshot_locked(job_id)
        if job is None:
            return None
        payload: dict = {
            "type": event_type,
            "created_at": iso(now()),
            "job": job,
        }
        started, finished = job.get("started"), job.get("finished")
        if started and finished:
            payload["duration_seconds"] = round(float(finished) - float(started), 3)
        base = (self.dashboard_base_url or "").rstrip("/")
        if base:
            payload["dashboard_url"] = f"{base}/#/job/{job['id']}"
        cursor = self.conn.execute(
            "INSERT INTO events(ts, job_id, type, data) VALUES(?,?,?,?)",
            (now(), job_id, event_type, json_dumps(payload)))
        seq = int(cursor.lastrowid or 0)
        payload["event_id"] = seq
        data = json_dumps(payload)
        self.conn.execute("UPDATE events SET data=? WHERE seq=?", (data, seq))
        urls: list[str] = []
        if event_type in ("job.started", "job.finished"):
            urls = list(dict.fromkeys(
                list(self.webhook_urls) + list(job.get("webhooks") or [])))
        elif webhook_urls:
            urls = list(dict.fromkeys(webhook_urls))
        for url in urls:
            self.conn.execute(
                "INSERT OR IGNORE INTO webhook_deliveries(job_id, event, url, "
                "msg_id, payload, status, attempts, next_attempt, created, "
                "updated) VALUES(?,?,?,?,?,'pending',0,0,?,?)",
                (job_id, event_type, url, f"msg_{uuid.uuid4().hex}", data,
                 now(), now()))
        self._bump()
        return seq

    def events_after(self, after_seq: int, job_id: str | None = None,
                     limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM events WHERE seq>?"
        args: list = [int(after_seq)]
        if job_id:
            sql += " AND job_id=?"
            args.append(job_id)
        sql += " ORDER BY seq LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [{"seq": row["seq"], "ts": row["ts"], "job_id": row["job_id"],
                 "type": row["type"], "data": json_loads(row["data"], {})}
                for row in rows]

    def last_event_seq(self) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM events").fetchone()
        return int(row["n"])

    def prune_events(self, keep: int = 10000) -> int:
        keep = max(0, int(keep))
        with self.lock:
            cursor = self.conn.execute(
                "DELETE FROM events WHERE seq <= (SELECT MAX(seq) - ? FROM events)",
                (keep,))
            return cursor.rowcount

    # ------------------------------------------------------------ webhook 投递
    def due_webhook_deliveries(self, now_ts: float | None = None,
                               limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM webhook_deliveries WHERE status='pending' "
                "AND next_attempt<=? ORDER BY next_attempt, id LIMIT ?",
                (now_ts if now_ts is not None else now(), int(limit))).fetchall()
        return [dict(row) for row in rows]

    def webhook_attempt(self, delivery_id: int) -> int | None:
        """投递前先落库 attempts+1；返回新的 attempts。

        记录已经不在（取批之后被撤掉，例如任务放回队列时撤回 job.started）
        返回 None：调用方不能再发送这条旧载荷。
        """
        with self.lock:
            cursor = self.conn.execute(
                "UPDATE webhook_deliveries SET attempts=attempts+1, updated=? "
                "WHERE id=? AND status='pending'", (now(), delivery_id))
            if cursor.rowcount == 0:
                return None
            row = self.conn.execute(
                "SELECT attempts FROM webhook_deliveries WHERE id=?",
                (delivery_id,)).fetchone()
        return int(row["attempts"]) if row else None

    def webhook_delivered(self, delivery_id: int, status_code: int | None) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE webhook_deliveries SET status='delivered', "
                "last_status=?, last_error='', updated=? WHERE id=?",
                (status_code, now(), delivery_id))

    def webhook_retry_later(self, delivery_id: int, next_attempt: float,
                            status_code: int | None, error: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE webhook_deliveries SET next_attempt=?, last_status=?, "
                "last_error=?, updated=? WHERE id=?",
                (next_attempt, status_code, error[:500], now(), delivery_id))

    def webhook_failed(self, delivery_id: int, status_code: int | None,
                       error: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE webhook_deliveries SET status='failed', last_status=?, "
                "last_error=?, updated=? WHERE id=?",
                (status_code, error[:500], now(), delivery_id))

    def webhook_deliveries(self, job_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT job_id, event, url, msg_id, status, attempts, "
                "last_status, last_error, created, updated "
                "FROM webhook_deliveries WHERE job_id=? "
                "ORDER BY url, created, id", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def pending_webhook_urls(self) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT url FROM webhook_deliveries "
                "WHERE status='pending'").fetchall()
        return [row["url"] for row in rows]

    def prune_webhook_deliveries(self, older_than: float) -> int:
        with self.lock:
            cursor = self.conn.execute(
                "DELETE FROM webhook_deliveries WHERE status!='pending' "
                "AND updated<?", (older_than,))
            return cursor.rowcount

    def prune_idempotency(self, window: float) -> int:
        with self.lock:
            cursor = self.conn.execute(
                "DELETE FROM idempotency WHERE created<?", (now() - window,))
            return cursor.rowcount

    # ------------------------------------------------------------ 日志
    def append_log(self, job_id: str, data: bytes) -> tuple[int, bool]:
        """追加一块日志，返回 (起始偏移, 是否因超上限被截断)。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(end),0) AS e FROM logs WHERE job_id=?",
                (job_id,)).fetchone()
            start = int(row["e"])
            truncated = False
            if start >= self.log_cap:
                data = b""
                truncated = True
            elif start + len(data) > self.log_cap:
                data = data[: self.log_cap - start]
                truncated = True
            if data:
                end = start + len(data)
                self.conn.execute(
                    "INSERT INTO logs(job_id, start, end, data) VALUES(?,?,?,?)",
                    (job_id, start, end, sqlite3.Binary(data)))
                self.conn.execute("UPDATE jobs SET log_bytes=? WHERE id=?",
                                  (end, job_id))
            if truncated:
                note = f"log truncated at {self.log_cap} bytes"
                self.conn.execute(
                    "UPDATE jobs SET error=CASE "
                    "WHEN error LIKE '%log truncated%' THEN error "
                    "WHEN error='' THEN ? ELSE error || '; ' || ? END WHERE id=?",
                    (note, note, job_id))
            return start, truncated

    def read_logs(self, job_id: str, offset: int = 0) -> tuple[bytes, int]:
        """返回 (从 offset 起的字节, 新的 next_offset)。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT start, end, data FROM logs WHERE job_id=? AND end>? "
                "ORDER BY start", (job_id, offset)).fetchall()
        chunks = []
        for row in rows:
            data = bytes(row["data"])
            if row["start"] < offset:
                data = data[offset - row["start"]:]
            chunks.append(data)
        return b"".join(chunks), offset + sum(len(c) for c in chunks)

    # ------------------------------------------------------------ 采样
    def add_sample(self, job_id: str, cpu_usec: int, mem_current: int,
                   mem_peak: int) -> None:
        with self.lock:
            prev = self.conn.execute(
                "SELECT ts, cpu_usec FROM samples WHERE job_id=? "
                "ORDER BY ts DESC LIMIT 1", (job_id,)).fetchone()
            ts = now()
            cores = 0.0
            if prev is not None:
                dt = ts - prev["ts"]
                delta = int(cpu_usec) - int(prev["cpu_usec"])
                if dt > 0 and delta > 0:
                    cores = delta / 1_000_000.0 / dt
            self.conn.execute(
                "INSERT INTO samples(job_id, ts, cpu_usec, mem_current, mem_peak) "
                "VALUES(?,?,?,?,?)", (job_id, ts, cpu_usec, mem_current, mem_peak))
            self.conn.execute(
                "UPDATE jobs SET cpu_total_sec=MAX(cpu_total_sec, ?), "
                "cpu_peak_cores=MAX(cpu_peak_cores, ?), "
                "mem_peak=MAX(mem_peak, ?) WHERE id=?",
                (cpu_usec / 1_000_000.0, cores, mem_peak, job_id))

    def samples(self, job_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, cpu_usec, mem_current, mem_peak FROM samples "
                "WHERE job_id=? ORDER BY ts", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 产物
    def add_artifact(self, job_id: str, name: str, size: int, sha256: str,
                     stored: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO artifacts(job_id, name, size, sha256, stored) "
                "VALUES(?,?,?,?,?) ON CONFLICT(job_id, name) DO UPDATE SET "
                "size=excluded.size, sha256=excluded.sha256, stored=excluded.stored",
                (job_id, name, size, sha256, stored))

    def artifacts(self, job_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT name, size, sha256, stored FROM artifacts WHERE job_id=? "
                "ORDER BY name", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def artifact(self, job_id: str, name: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT name, size, sha256, stored FROM artifacts "
                "WHERE job_id=? AND name=?", (job_id, name)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 维护
    def expired_jobs(self, now_ts: float, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM jobs WHERE cleanup_at IS NOT NULL AND cleanup_at<=? "
                "AND purged=0 AND status IN ('succeeded','failed','cancelled',"
                "'timeout','error') LIMIT ?", (now_ts, limit)).fetchall()
        return [dict(r) for r in rows]

    def purge_job(self, job_id: str) -> list[str]:
        """删除一个任务的日志/采样/产物记录与 bundle 行，返回要删的文件路径。"""
        files: list[str] = []
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self.conn.execute(
                    "SELECT stored FROM artifacts WHERE job_id=?", (job_id,)).fetchall()
                files.extend(row["stored"] for row in rows)
                bundle = self.conn.execute(
                    "SELECT b.path FROM bundles b JOIN jobs j ON j.bundle_id=b.id "
                    "WHERE j.id=?", (job_id,)).fetchone()
                if bundle:
                    files.append(bundle["path"])
                self.conn.execute("DELETE FROM logs WHERE job_id=?", (job_id,))
                self.conn.execute("DELETE FROM samples WHERE job_id=?", (job_id,))
                self.conn.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))
                # 兼容两种关联写法：bundle 行上的 job_id，或 jobs.bundle_id。
                self.conn.execute(
                    "DELETE FROM bundles WHERE job_id=? OR id=("
                    "SELECT bundle_id FROM jobs WHERE id=?)", (job_id, job_id))
                self.conn.execute(
                    "UPDATE jobs SET purged=1, cleanup_at=NULL WHERE id=?", (job_id,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return files

    def purge_orphan_bundles(self, now_ts: float, max_age: float = 86400) -> list[str]:
        """清掉上传后没有绑定任务的 bundle（客户端中途失败留下的）。"""
        files: list[str] = []
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, path FROM bundles WHERE job_id='' AND created<?",
                (now_ts - max_age,)).fetchall()
            for row in rows:
                files.append(row["path"])
                self.conn.execute("DELETE FROM bundles WHERE id=?", (row["id"],))
        return files

    def checkpoint(self) -> None:
        """把 WAL 合并回主库并截断（janitor 每轮调用一次）；失败不算错，下一轮再来。

        必须持锁，并且在锁里把结果行取完。``PRAGMA wal_checkpoint`` 是一条会返回
        一行结果的写语句：从它返回到游标被释放之间一直算「进行中」，这期间同一
        连接上的 COMMIT 会报 ``cannot commit transaction - SQL statements in
        progress``。以前这里不持锁，janitor 线程拿到结果后只要晚一点被调度（机器
        负载高时），请求线程的事务就会撞上；反过来别的线程事务没结束时这条 PRAGMA
        也只会报 ``database table is locked``，白跑一轮。
        """
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                pass

    def stats(self) -> dict:
        with self.lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
