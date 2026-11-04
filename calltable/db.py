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
import threading
from pathlib import Path

from .util import json_dumps, json_loads, new_id, now

#: 主机心跳超时（秒）。15 分钟：网络抖动/短暂挂起不应该误杀任务。
HOST_STALE_SECONDS = 900
#: 连续多少次心跳没有报告某任务就回收。
MISSING_STRIKES = 2
#: 单任务日志硬上限。
DEFAULT_LOG_CAP = 256 * 1024 * 1024

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
]

RUNNING = "running"
QUEUED = "queued"
TERMINAL = ("succeeded", "failed", "cancelled", "timeout", "error")


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


class Store:
    """每个进程一个连接；hub 是 ThreadingHTTPServer，用一个锁串行化写事务。"""

    def __init__(self, db_path: str | Path, log_cap: int = DEFAULT_LOG_CAP):
        self.path = str(db_path)
        self.log_cap = int(log_cap)
        self.lock = threading.RLock()
        self.conn = connect(db_path)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

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
                  system: dict | None = None) -> list[dict]:
        """记录一次心跳并立即对账；返回被回收的任务。

        对账规则见模块文档。``running`` 必须是该执行器实例当前持有的 job id。
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

    def reap_stale_hosts(self, stale_seconds: int = HOST_STALE_SECONDS) -> list[dict]:
        """主机心跳超时：回收它名下的 running 任务。"""
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
                   group_name: str = "", targets: list[str] | None = None) -> dict:
        job_id = new_id()
        with self.lock:
            self.conn.execute(
                """INSERT INTO jobs(id, project, host, slot, locks, cmd, profile,
                       submitter, agent, label, sha, base_ref, base_ref_sha,
                       base_sha, dirty, status, created, timeout, bundle_id,
                       group_name, targets)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, project, host, slot, json_dumps(locks), json_dumps(cmd),
                 profile, submitter, agent, label, sha, base_ref, base_ref_sha,
                 base_sha, 1 if dirty else 0, QUEUED, now(), timeout, bundle_id,
                 group_name, json_dumps(targets or [host])))
            if bundle_id:
                self.conn.execute("UPDATE bundles SET job_id=? WHERE id=?",
                                  (job_id, bundle_id))
        return self.job(job_id)

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
        data["locks_display"] = self._display_locks(
            data["locks"], data["targets"], data["host"], data["status"])
        data["dirty"] = bool(data["dirty"])
        data["cancel_requested"] = bool(data["cancel_requested"])
        data["needs_reconcile"] = bool(data.get("needs_reconcile"))
        data["purged"] = bool(data.get("purged"))
        data.pop("bundle_id", None)
        data.pop("misses", None)
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
            ahead = sum(1 for earlier in queued[:index]
                        if self._competes(earlier, job))
            info[job["id"]] = {
                "queued_seconds": max(0, int(now_ts - job["created"])),
                "ahead": ahead,
            }
        return info

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
                running = self.conn.execute(
                    "SELECT * FROM jobs WHERE status='running'").fetchall()
                used: dict[str, dict[str, int]] = {}
                for job in running:
                    bucket = used.setdefault(job["host"], {})
                    bucket[job["slot"]] = bucket.get(job["slot"], 0) + 1
                held_locks = self._held_lock_keys(running)
                now_ts = now()

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
                self.conn.execute(
                    "UPDATE jobs SET status='running', started=?, worker=?, "
                    "host=?, locks=?, misses=0, needs_reconcile=0 "
                    "WHERE id=? AND status='queued'",
                    (now(), worker, selected_host, json_dumps(locks),
                     chosen["id"]))
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
                elif job["status"] == RUNNING:
                    self.conn.execute(
                        "UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.job(job_id)

    def finish(self, job_id: str, *, status: str, exit_code: int | None,
               error: str = "", cleanup_at: float | None = None) -> dict | None:
        with self.lock:
            self._finish_locked(job_id, status, exit_code, error, cleanup_at)
        return self.job(job_id)

    def _finish_locked(self, job_id: str, status: str, exit_code: int | None,
                       error: str = "", cleanup_at: float | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, finished=?, error=?, "
            "cleanup_at=COALESCE(?, cleanup_at), needs_reconcile=0 WHERE id=?",
            (status, exit_code, now(), error, cleanup_at, job_id))

    def append_error(self, job_id: str, note: str) -> None:
        """给任务追加一条说明，不改变 status / exit_code。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT error FROM jobs WHERE id=?", (job_id,)).fetchone()
            existing = (row["error"] if row else "") or ""
            merged = (existing + "; " + note) if existing else note
            self.conn.execute("UPDATE jobs SET error=? WHERE id=?", (merged, job_id))

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
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def stats(self) -> dict:
        with self.lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
