"""执行器：常驻主机，按 host 名向 hub 领取任务并在本机跑完。

流程
----
启动 → 生成 worker 实例 id → 重放上次没送达的 finish → 循环（心跳 + claim）。
每个任务：下载 bundle（如有）→ fetch 进本机裸镜像 → ``git worktree add`` 到
``runs_root/<job_id>/work`` → 写 launcher（source profile 的 env 文件）→ 启动
（Linux 走 systemd scope，Windows 走 Job Object）→ 实时上传日志分块 → 每 10 秒
上传资源采样 → 收集产物（glob + sha256）→ 上报终态。

准备阶段（下载/fetch/worktree）也受同一个超时截止时间约束，并在每步前后检查
取消标记（I8）。取消时先礼貌终止再强杀整棵进程树。

终态在**上报之前**就原子写进 ``<data_dir>/pending/<job_id>.json``（临时文件 +
fsync + rename）：上报失败、任务线程被杀、执行器重启都不会丢结果（B1）。之后
指数退避重试（最长 10 分钟），仍然失败就留在 pending 里，由每一次心跳前的
有界重放接着送；写不进盘时结果退到内存副本，照样重放，退出才丢。同一个任务
同时只有一个线程在送这份结果，重放和任务线程不会把同一份发两遍。重启后的执行器
会先有界地（:data:`INHERITED_RESULT_GRACE`，默认 5 分钟）把上一个实例留下的结果
送出去，再发第一次心跳——hub 对 worker 变化是无条件回收的，心跳一发就晚了。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from . import __version__, disk, gitops, proc, proc_init
from .client import HubClient, HubError
from .config import Config
from .util import now, parse_size, valid_project_name

LOG_CHUNK = 32 * 1024
LOG_FLUSH_SECONDS = 0.8
MAX_ARTIFACTS = 200
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
ARTIFACT_SCAN_SECONDS = 60.0
# 产物收集不能把整个 node_modules 走一遍：在 Windows 上实测 >10 分钟，
# 主进程早就退出了，job 却一直挂着。命中这些目录名直接剪枝。
ARTIFACT_SKIP_DIRS = {
    ".git", "node_modules", ".pnpm", ".cache", ".turbo-build", ".build-cache",
    ".turbo", ".next", ".venv", "venv", "target", "dist", "coverage",
}
FINISH_BUDGET = 600.0
FINISH_MAX_DELAY = 30.0
#: 心跳前那一轮重放的上限：整轮最多这么久、最多试这么多份、最多看这么多候选。
#: 重放绝不能把心跳拖住，剩下的留给下一次心跳（默认 poll_interval 3 秒，通常
#: 一秒内就送完）。候选每轮从上一轮停下的位置开始轮转，所以积压的结果不会被
#: 前面那些正在退避的结果永远挡住。
REPLAY_BUDGET = 10.0
REPLAY_MAX_FILES = 50
REPLAY_MAX_WALK = 200
#: 一次性执行器（Cloud Run）退出前那一轮：容器马上要回收，给足时间。
REPLAY_FINAL_BUDGET = 120.0
#: 启动时继承的、还没送出去的终态：有界推迟第一次心跳（和领任务）最多这么久，
#: 好让这些结果先送达。hub 的 ``Store.heartbeat`` 对 worker 变化是无条件回收的
#: （db.py:407 先判 ``job["worker"] != worker``，之后才看 ``running``），所以只把
#: 任务报进 ``running`` 救不了它们。窗口必须小于 ``HOST_STALE_SECONDS``（900s）。
INHERITED_RESULT_GRACE = 300.0
#: 推迟期间两次重放之间等多久。
INHERITED_HOLD_PAUSE = 2.0
#: hub 明确答复、重试也不会变的失败（4xx，除 408/429）按任务退避：
#: 30s 起、翻倍、最长 10 分钟，免得每 3 秒打一次 hub。
REPLAY_BACKOFF = 30.0
REPLAY_BACKOFF_MAX = 600.0
#: 还没 rename 的 pending 临时文件后缀：``.<job id>.json.tmp``。
PENDING_TMP_SUFFIX = ".tmp"
#: 磁盘/runs 大小的全量扫描至少隔这么久（walk 巨型 node_modules 很贵）。
DISK_SCAN_INTERVAL = 60.0
#: 调整任务进程 OOM 权重的 procfs 入口（只在 Linux 上存在）。
OOM_SCORE_ADJ_PATH = "/proc/self/oom_score_adj"
#: 只给执行器自己用的 hub 凭据：不随环境变量传给任务命令（Cloud Run 上它们
#: 由 Secret Manager 注入容器环境）。``CALLTABLE_GIT_TOKEN`` 是从远端仓库
#: （GitHub）取代码用的只读 token。
HUB_CREDENTIAL_ENV = ("CALLTABLE_TOKEN", "TS_AUTHKEY", "CALLTABLE_GIT_TOKEN")
#: 从远端仓库取 hub 推过去的临时 ref 的超时上限（秒）。另外最多只用剩余预算的
#: 一半：远端卡住时超时后还有时间回退到 hub bundle，而不是整个任务超时。
GIT_RELAY_FETCH_TIMEOUT = 300.0
#: 剩余预算的一半不到这么多秒就不试远端，直接走 hub bundle。
GIT_RELAY_MIN_TIMEOUT = 1.0
#: hub 推送的临时 ref 前缀（``refs/calltable/jobs/<job id>``）。
_GIT_RELAY_REFS_RE = re.compile(r"^refs/calltable/jobs/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _fsync_dir(path: Path) -> None:
    """rename 之后把目录项也刷到盘上（POSIX）；Windows 上打不开目录，跳过。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _hub_unavailable(exc: HubError) -> bool:
    """hub 这会儿收不了（连不上、超时、5xx、408、429）：这一轮重放立刻收手。

    这种情况下继续送下一份只是排队等超时，心跳会被拖住；等 hub 回来时，
    下一轮重放会把它们一起送出去。
    """
    return exc.status is None or exc.status >= 500 or exc.status in (408, 429)


def _finish_rejected(exc: HubError) -> bool:
    """hub 明确拒收这份终态（只在响应里带 ``finish_rejected`` 时才算）。

    代理或路由没对上的 404、401/403（修好 token 还能重放）、5xx 都不算，照旧
    留给重放；带标记的 4xx 重试也不会有别的结果，本地结果直接丢掉。没有这个
    标记的 hub（0.2、0.3）行为不变：一直留着重放。
    """
    payload = exc.payload if isinstance(exc.payload, dict) else None
    return (exc.status is not None and 400 <= exc.status < 500
            and bool(payload) and payload.get("finish_rejected") is True)


def _artifact_regex(pattern: str) -> re.Pattern[str]:
    """把产物 glob 转成正则（``**`` 跨目录，``**/`` 可匹配零层）。

    一段连续的 ``*`` 只生成一个通配，不再叠出会回溯的多个量词。匹配的集合与
    原来逐对翻译（``**`` -> ``.*``，``**/`` -> ``(?:.*/)?``，剩一个 ``*`` ->
    ``[^/]*``）完全相同：

    - ``*`` -> ``[^/]*``；两个及以上 -> ``.*``；
    - 后面紧跟 ``/`` 且个数为偶数时，最后一对原来是 ``**/``，``/`` 可以不出现：
      恰好两个 -> ``(?:.*/)?``，四个及以上 -> ``.*``（``.*(?:.*/)?`` 等于
      ``.*``），都把这个 ``/`` 一并吃掉；个数为奇数时 ``/`` 仍按字面匹配。
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            end = i
            while end < len(pattern) and pattern[end] == "*":
                end += 1
            run = end - i
            slash = pattern.startswith("/", end) and run % 2 == 0
            if run == 1:
                out.append("[^/]*")
            elif run == 2 and slash:
                out.append("(?:.*/)?")
            else:
                out.append(".*")
            i = end + 1 if slash else end
            continue
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.compile("".join(out))

class Aborted(Exception):
    """准备阶段被取消或超时。"""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class Executor:
    def __init__(self, config: Config, *, once: bool = False,
                 client: HubClient | None = None, job_id: str | None = None):
        self.config = config
        #: ``--job-id``：一次性执行器（Cloud Run）只认领 hub 为它预留的这条任务。
        self.job_id = (job_id or "").strip() or None
        self.once = once or bool(self.job_id)
        #: 0 正常；attach 失败时非 0（让 Cloud Run execution 记为失败）。
        self.exit_code = 0
        self.host = config.exec_host
        self.worker_id = f"{self.host}-{uuid.uuid4().hex[:10]}"
        self.client = client or HubClient(config.hub_url, config.token_value,
                                          proxy=config.hub_proxy)
        self.runs_root = config.runs_root
        self.mirror_root = config.mirror_root
        self.pending_dir = config.data_dir / "pending"
        if os.name != "nt":
            # 执行器创建的所有目录/文件（含 git clone 的检出）都必须是
            # owner-only；LocalStorage 拒绝组可写的目录链。进程级 umask 比
            # 事后递归 chmod 便宜得多。
            os.umask(0o077)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.mirror_root.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        # 任务临时目录会被 LocalStorage 的 owner-only 检查拒绝，如果祖先目录
        # 组可写（某些发行版默认 umask 0002）。启动时把 runs 目录链收紧。
        self._tighten_private_dirs()

    @staticmethod
    def _tighten_private_dirs_for(path: Path) -> None:
        if os.name == "nt":
            return
        for target, mode in ((path, 0o700), (path.parent, 0o755)):
            try:
                if target.exists():
                    current = target.stat().st_mode & 0o777
                    if current & 0o022:
                        os.chmod(target, mode)
            except OSError:
                pass

    def _tighten_private_dirs(self) -> None:
        self._tighten_private_dirs_for(self.runs_root)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._active: dict[str, threading.Thread] = {}
        self._active_jobs: set[str] = set()
        #: 已经跑完但终态还没送达 hub 的任务：仍要在心跳里报告，否则 hub 会把它
        #: 当成「丢失」回收，把成功改成 error（hub 重启窗口里真实发生过）。
        self._unreported: set[str] = set()
        #: 写不进 pending 目录（磁盘满、只读、序列化失败）的终态：只在内存里，
        #: 照样重放，执行器退出就丢（写失败时会打日志说明）。
        self._unpersisted: dict[str, dict] = {}
        #: 正在上报的 job id：任务线程和心跳前的重放不能同时送同一份结果。
        self._delivering: set[str] = set()
        #: hub 明确答复过的失败：job id → (下次可以重放的时刻, 当前退避秒数)。
        self._replay_after: dict[str, tuple[float, float]] = {}
        #: 重放候选的轮转游标：每轮从上一轮停下的位置开始，后面的结果也轮得到。
        self._replay_cursor = 0
        #: 半截临时文件的恢复只能在启动时、任何任务线程开始写 pending 之前做一次。
        self._pending_recovered = False
        #: 启动时盘上还没送出去的终态（上一个执行器实例留下的）：第一次心跳要等
        #: 它们送到（最多 INHERITED_RESULT_GRACE 秒）。
        self._inherited: set[str] = set()
        self._cancel_flags: dict[str, bool] = {}
        self._watcher: threading.Thread | None = None
        self._disk_status: disk.DiskStatus = disk.DiskStatus()
        self._disk_checked_at = 0.0
        #: 已完成 run 目录的大小缓存：避免每分钟重新 walk node_modules。
        self._run_size_cache: dict[str, tuple[float, int]] = {}
        self._disk_warned_at = 0.0
        self._disk_cleanup_lock = threading.Lock()
        self._disk_cleanup_thread: threading.Thread | None = None
        #: janitor 和按需清理都会调 cleanup_disk；同一时刻只让一份在删。
        self._disk_cleanup_running = threading.Lock()
        self._disk_undeletable_logged: list[str] = []

        self.use_systemd, self.systemd_reason = self._decide_systemd()

    # ------------------------------------------------------------ 启动
    def _decide_systemd(self) -> tuple[bool, str]:
        want = self.config.raw.get("use_systemd", None)
        if want is False:
            return False, "disabled by config"
        available, reason = proc.probe_systemd()
        if available:
            return True, "probe ok"
        if reason == "windows":
            return False, "windows"
        return False, f"probe failed: {reason}; falling back to direct mode"

    def run(self) -> None:
        # 先补完上次崩溃留下的半截临时文件，再开始读 pending / 起任务线程。
        self._recover_partial_pending_once()
        # 记下上一实例留下的结果：第一次心跳之前要尽量把它们送出去。
        self._snapshot_inherited_results()
        self._reclaim_orphans()
        print(f"calltable exec host={self.host} worker={self.worker_id} "
              f"hub={self.config.hub_url} runs={self.runs_root} "
              f"systemd={self.use_systemd} ({self.systemd_reason})", flush=True)
        if not self.use_systemd and self.systemd_reason.startswith("probe failed"):
            print(f"[exec] WARNING: {self.systemd_reason}", file=sys.stderr,
                  flush=True)
        # 继承了上一个实例的结果时，先有界地把它们送出去再发第一次心跳：
        # hub 对 worker 变化是无条件回收的，心跳一发结果就变成「迟到」。
        self._replay_pending_quietly()
        self._await_inherited_results()
        self._start_watcher()
        self._start_housekeeping()
        if self.once:
            self._run_once()
            return
        while not self._stop.is_set():
            try:
                if not self.heartbeat():
                    self._stop.wait(self.config.poll_interval)
                    continue
                self._claim_loop()
            except HubError as exc:
                print(f"[hub] {exc}", file=sys.stderr, flush=True)
            except Exception as exc:  # pragma: no cover - 常驻兜底
                print(f"[exec] {type(exc).__name__}: {exc}", file=sys.stderr,
                      flush=True)
            self._stop.wait(self.config.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            procs = list(self._running_procs.values())
        for tree in procs:
            tree.terminate(3)

    _running_procs: dict[str, proc.TreeProcess] = {}

    def _start_watcher(self) -> None:
        self._watcher = threading.Thread(target=self._watch_loop, daemon=True,
                                         name="exec-watcher")
        self._watcher.start()

    def _watch_loop(self) -> None:
        """每 3 秒查一次在跑任务是否被取消（准备阶段也用它）。"""
        while not self._stop.wait(3):
            with self._lock:
                jobs = list(self._active_jobs)
            for job_id in jobs:
                # hub 重启时会直接断开连接（RemoteDisconnected）或迟迟不答
                # （TimeoutError），这些不是 HubError；只跳过这一轮，不能让
                # 轮询线程就此退出、之后再也看不到取消。
                try:
                    state = self.client.get_json(f"/api/jobs/{job_id}")
                except (HubError, OSError, ValueError):
                    continue
                if isinstance(state, dict) and state.get("cancel_requested"):
                    self._cancel_flags[job_id] = True

    def _start_housekeeping(self) -> None:
        thread = threading.Thread(target=self._janitor_loop, daemon=True,
                                  name="exec-janitor")
        thread.start()

    def _janitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.cleanup_disk()
                self.janitor()
                # 上次没送达的终态：定期重试，别等下一次重启。
                if self._has_unsent_results():
                    self.replay_pending()
            except Exception as exc:  # pragma: no cover
                print(f"[exec] janitor: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
            self._stop.wait(600)

    # ------------------------------------------------------------ 心跳/领取
    def heartbeat(self) -> bool:
        # 先送没送达的终态（B1）：hub 一恢复，结果在下一次心跳之前就到了，
        # 槽位和锁马上释放。有界重放，绝不因为重放把心跳拖住或抛异常。
        self._replay_pending_quietly()
        with self._lock:
            running = sorted(self._active_jobs | self._unreported)
        system = self._host_system()
        try:
            self.client.post_json("/api/hosts/heartbeat", {
                "host": self.host,
                "worker": self.worker_id,
                "running": running,
                "os": f"{sys.platform}-{os.name}",
                "version": __version__,
                "system": system,
            })
            return True
        except HubError as exc:
            print(f"[hub] heartbeat failed: {exc}", file=sys.stderr, flush=True)
            return False

    # ------------------------------------------------------------ 磁盘水位
    def disk_status(self, *, force: bool = False) -> disk.DiskStatus:
        """测量 runs_root 空闲与 runs 大小（只读，不删目录）。

        删除几十 GB 的 run 目录可能要好几分钟，绝不能放在心跳路径上，
        否则主机在 hub 眼里会失联。清理见 :meth:`cleanup_disk`。
        """
        now_ts = time.time()
        interval = 15.0 if force else DISK_SCAN_INTERVAL
        if (not self._disk_checked_at
                or now_ts - self._disk_checked_at >= interval):
            self._disk_status = disk.measure(
                self.runs_root,
                min_free_gb=self.config.min_free_gb,
                runs_max_gb=self.config.runs_max_gb,
                cache=self._run_size_cache,
                scan_budget=self.config.disk_scan_budget)
            self._disk_checked_at = now_ts
        return self._disk_status

    def cleanup_disk(self) -> disk.DiskStatus:
        """把 runs 清理到水位以下（慢操作；放在后台线程或 janitor 里跑）。

        janitor 和按需清理可能同时触发；已经有一份在删时直接返回上次的状态，
        不要两个线程抢着 rmtree 同一批目录。
        """
        if not self._disk_cleanup_running.acquire(blocking=False):
            return self._disk_status
        try:
            with self._lock:
                protected = set(self._active_jobs)
            status = disk.enforce(
                self.runs_root,
                min_free_gb=self.config.min_free_gb,
                runs_max_gb=self.config.runs_max_gb,
                protected=protected,
                cache=self._run_size_cache,
                scan_budget=self.config.disk_scan_budget)
        finally:
            self._disk_cleanup_running.release()
        self._disk_status = status
        self._disk_checked_at = time.time()
        if status.reclaimed:
            print(f"[exec] disk watermark: removed "
                  f"{status.reclaimed} finished run dir(s), "
                  f"freed {status.reclaimed_bytes / disk.GB:.1f}G", flush=True)
        stuck = sorted(status.undeletable)
        if stuck and stuck != self._disk_undeletable_logged:
            shown = ", ".join(stuck[:5]) + (" ..." if len(stuck) > 5 else "")
            print(f"[exec] disk watermark: cannot fully remove {len(stuck)} "
                  f"run dir(s) under {self.runs_root} (files owned by another "
                  f"user?), skipped: {shown}", file=sys.stderr, flush=True)
        self._disk_undeletable_logged = stuck
        return status

    def request_disk_cleanup(self) -> bool:
        """单飞地启动一次后台清理；已在跑就返回 False。"""
        with self._disk_cleanup_lock:
            if self._disk_cleanup_thread and self._disk_cleanup_thread.is_alive():
                return False
            thread = threading.Thread(target=self._disk_cleanup_worker,
                                      daemon=True, name="exec-disk-cleanup")
            self._disk_cleanup_thread = thread
            thread.start()
            return True

    def _disk_cleanup_worker(self) -> None:
        try:
            self.cleanup_disk()
        except Exception as exc:  # pragma: no cover - 后台兜底
            print(f"[exec] disk cleanup: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    def _host_system(self) -> dict:
        status = self.disk_status()
        payload = status.to_dict()
        total, available = disk.memory_bytes()
        payload.update({
            "mem_total_gb": round(total / disk.GB, 1),
            "mem_free_gb": round(available / disk.GB, 1),
            "load1": disk.load_average(),
        })
        return payload

    def _claim_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                live = {k: t for k, t in self._active.items() if t.is_alive()}
                self._active = live
                self._active_jobs = {k for k in self._active_jobs if k in live}
                if len(live) >= self.config.max_jobs:
                    return
            status = self.disk_status(force=bool(self._disk_status.low))
            if status.low:
                # 清理放到后台线程，心跳/对账不能被 rmtree 拖住。
                self.request_disk_cleanup()
                now_ts = time.time()
                if now_ts - self._disk_warned_at > 300:
                    self._disk_warned_at = now_ts
                    print(f"[exec] disk_low: {status.reason}; not claiming "
                          "new jobs", file=sys.stderr, flush=True)
                return
            if self.job_id:
                try:
                    job = self.client.post_json(
                        f"/api/exec/jobs/{self.job_id}/attach",
                        {"host": self.host, "worker": self.worker_id})
                except HubError as exc:
                    print(f"[exec] attach {self.job_id} failed: {exc}",
                          file=sys.stderr, flush=True)
                    self.exit_code = 3
                    return
                if not job:
                    self.exit_code = 3
                    return
            else:
                job = self.client.post_json("/api/exec/claim", {
                    "host": self.host, "worker": self.worker_id})
            if not job:
                return
            thread = threading.Thread(target=self._run_job, args=(job,),
                                      name=f"job-{job['id']}", daemon=True)
            with self._lock:
                self._active[job["id"]] = thread
                self._active_jobs.add(job["id"])
                # 领取和取消几乎同时发生：hub 返回的快照里已经带了取消请求，
                # 不等取消轮询（3 秒一次），准备阶段第一个检查点就中止。
                if job.get("cancel_requested"):
                    self._cancel_flags[job["id"]] = True
            thread.start()
            if self.once:
                thread.join()
                return

    def _reclaim_orphans(self) -> None:
        """执行器重启后，杀掉上一次进程留下的任务进程并结算。

        Python 进程被杀时，子进程（bash/dotnet）可能继续跑；hub 会把这些
        任务标成 needs_reconcile，但如果不主动清理，它们会一直占着槽位且
        永远不会触发超时。"""
        try:
            entries = list(self.runs_root.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            marker = entry / ".running"
            if not marker.exists():
                continue
            try:
                owner = marker.read_text(encoding="utf-8").strip()
            except OSError:
                owner = ""
            if owner == self.worker_id:
                continue
            job_id = entry.name
            pid_text = ""
            try:
                pid_text = (entry / ".pid").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if pid_text.isdigit() and self._pid_matches(int(pid_text), job_id):
                self._kill_tree(int(pid_text), job_id)
            pending = self._pending_result(job_id)
            if pending is None:
                try:
                    self.client.post_json(
                        f"/api/exec/jobs/{job_id}/finish",
                        {"status": "error", "exit_code": None,
                         "error": "executor restarted; orphan job terminated"})
                except HubError:
                    pass
                self._schedule_cleanup(entry, "error")
            else:
                # 上一个实例已经把终态落盘：真实结果交给心跳前的重放去送，
                # 别再补一个 error 盖掉它。保留期按那份结果的状态算。
                self._schedule_cleanup(entry, str(pending.get("status") or "error"))
            try:
                marker.unlink()
            except OSError:
                pass

    def _pid_matches(self, pid: int, job_id: str) -> bool:
        """PID 可能被系统复用；只杀命令行里确实属于该任务的进程。"""
        try:
            if os.name == "nt":
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
                     ".CommandLine"],
                    capture_output=True, text=True, timeout=20)
                return job_id in (out.stdout or "")
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return job_id in raw.replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return False

    def _kill_tree(self, pid: int, job_id: str) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=30)
            else:
                from .proc_linux import unit_name
                subprocess.run(["systemctl", "--user", "stop",
                                unit_name(job_id) + ".scope"],
                               capture_output=True, timeout=30)
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        except (OSError, subprocess.SubprocessError):
            pass

    def _run_once(self) -> None:
        # 幂等：``run()`` 已经做过一次，直接调 ``_run_once``（测试、嵌入）时补上。
        self._recover_partial_pending_once()
        self.heartbeat()
        if not self.job_id:
            self._claim_loop()
            return
        # 一次性执行器：任务跑多久就要心跳多久，否则 hub/provider 会当它失联。
        done = threading.Event()
        interval = max(1.0, min(30.0, self.config.poll_interval * 10))

        def beat() -> None:
            while not done.wait(interval):
                self.heartbeat()

        beater = threading.Thread(target=beat, daemon=True, name="exec-beat")
        beater.start()
        try:
            self._claim_loop()
        finally:
            done.set()
            beater.join(timeout=5)
        # 终态没送达就再试一次（容器马上要回收，pending 文件留不下来）。
        if self._has_unsent_results():
            self._replay_pending_quietly(budget=REPLAY_FINAL_BUDGET)
        self.heartbeat()

    # ------------------------------------------------------------ 单个任务
    def _run_job(self, job: dict) -> None:
        job_id = job["id"]
        run_dir = self.runs_root / job_id
        work = run_dir / "work"
        tmp = run_dir / "tmp"
        run_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(run_dir, 0o700)
            except OSError:
                pass
        (run_dir / ".running").write_text(self.worker_id, encoding="utf-8")
        work.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            for private in (work, tmp):
                try:
                    os.chmod(private, 0o700)
                except OSError:
                    pass
        profile = job.get("profile_data") or {}
        timeout = int(job["timeout"]) if job.get("timeout") is not None else 3600
        deadline = time.time() + timeout
        outcome = {"status": "error", "exit_code": None, "error": ""}
        tree = None
        try:
            mirror = self._mirror_for(job["project"])
            sha = job.get("sha") or ""
            if not sha:
                raise RuntimeError("job has no commit sha")

            self._checkpoint(job_id, deadline)
            # Cloud Run + cloudrun.git：先从远端仓库取 hub 推过去的临时 ref；
            # 提交到手就不再从 hub 下载 bundle（hub 链路慢），否则照旧走 bundle。
            relayed = False
            if (os.environ.get("CALLTABLE_GIT_URL")
                    and os.environ.get("CALLTABLE_GIT_REFS")):
                relayed = self._fetch_git_relay(job_id, mirror, sha, deadline)
                self._checkpoint(job_id, deadline, "git relay fetch")
            if job.get("bundle_url") and not relayed:
                bundle = run_dir / "upload.bundle"
                self.client.download(job["bundle_url"], bundle)
                self._checkpoint(job_id, deadline, "bundle download")
                try:
                    gitops.ingest_bundle(mirror, bundle)
                except gitops.GitError:
                    # 增量包的前置提交不在本机镜像里（新执行器、Cloud Run 的全新
                    # 容器）：改取整包。先删掉增量包——download 见到同名文件会带
                    # Range 续传，把整包的后半段追加到增量包后面，文件头仍是增量包。
                    bundle.unlink(missing_ok=True)
                    self.client.download(job["bundle_url"] + "?full=1", bundle)
                    gitops.ingest_bundle(mirror, bundle)
                bundle.unlink(missing_ok=True)
            if not gitops.has_commit(mirror, sha):
                bundle = run_dir / "full.bundle"
                self.client.download(f"/api/jobs/{job_id}/bundle?full=1", bundle)
                gitops.ingest_bundle(mirror, bundle)
                bundle.unlink(missing_ok=True)
            self._checkpoint(job_id, deadline, "bundle fetch")
            if not gitops.has_commit(mirror, sha):
                raise RuntimeError(f"commit {sha} is not available in the executor mirror")

            # 每个任务独立 clone --shared：不用 worktree，避免并发任务共享镜像
            # refs 时互相覆盖 refs/remotes/origin/main。
            base_ref_sha = job.get("base_ref_sha") or ""
            if base_ref_sha and not gitops.has_commit(mirror, base_ref_sha):
                bundle = run_dir / "base.bundle"
                self.client.download(
                    f"/api/jobs/{job_id}/bundle?full=1&sha={base_ref_sha}", bundle)
                gitops.ingest_bundle(mirror, bundle)
                bundle.unlink(missing_ok=True)
            gitops.clone_shared(mirror, work, sha)
            if os.name != "nt":
                # clone_shared 会重建 work，权限需要在 clone 之后再收紧一次。
                try:
                    os.chmod(work, 0o700)
                except OSError:
                    pass
            self._checkpoint(job_id, deadline, "checkout")
            if base_ref_sha and job.get("base_ref"):
                gitops.set_base_ref(work, job["base_ref"], base_ref_sha)

            script = self._write_launcher(run_dir, job, profile, work)
            env = self._build_env(job, profile, tmp, work)
            argv = self._argv(script)
            # 取消轮询 3 秒一次；拉起进程前直接问一次 hub，准备期间到达的
            # 取消不会让进程再跑起来。
            self._refresh_cancel(job_id)
            self._checkpoint(job_id, deadline, "before launch")

            tier = job.get("systemd") or {}
            tree = proc.spawn(
                argv=argv, cwd=work, env=env, job_id=job_id,
                use_systemd=self.use_systemd,
                memory_max=tier.get("memory_max"),
                cpu_weight=tier.get("cpu_weight"),
                slice=tier.get("slice"),
                cpu_rate_percent=(job.get("cpu_rate_percent")
                                  or self.config.cpu_rate_percent),
                soft_memory_bytes=parse_size(tier.get("memory_max")),
            )
            try:
                (run_dir / ".pid").write_text(str(tree.popen.pid), encoding="utf-8")
            except OSError:
                pass
            with self._lock:
                self._running_procs[job_id] = tree

            pump = LogPump(self.client, job_id, tree.popen.stdout)
            pump.start()
            sampler = threading.Thread(target=self._sample_loop,
                                       args=(job_id, tree), daemon=True)
            sampler.start()

            status, limit_error = self._supervise(job, tree, job_id)
            try:
                code = tree.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # 主进程已退出但孙进程还继承着管道：给日志 10 秒收尾，
                # 然后结束整个进程组/Job，绝不能让它把任务拖住。
                tree.terminate(3)
                code = tree.wait(timeout=10)
            # 主进程已退出时，孙进程可能还活着并占着 stdout。无条件收掉
            # 整个进程组/Job，管道才会 EOF；不能让后台进程拖住收尾。
            tree.terminate(2)
            pump.finish()
            sampler.join(timeout=5)
            outcome = {"status": status, "exit_code": code, "error": limit_error}

            notes = self._collect_artifacts(job_id, work, profile.get("artifacts") or [])
            if notes:
                extra = "; ".join(notes)
                outcome["error"] = (f"{outcome['error']}; {extra}"
                                   if outcome["error"] else extra)
        except Aborted as exc:
            outcome = {"status": exc.status, "exit_code": None,
                       "error": "aborted during preparation"}
            if tree is not None:
                tree.terminate(3)
        except Exception as exc:
            # 已经收到取消请求时，准备阶段的失败（多半就是被取消打断的）
            # 照样报 cancelled，原始错误留在说明里。
            cancelled = bool(self._cancel_flags.get(job_id))
            outcome = {"status": "cancelled" if cancelled else "error",
                       "exit_code": None,
                       "error": (f"cancel requested; {type(exc).__name__}: {exc}"
                                 if cancelled
                                 else f"{type(exc).__name__}: {exc}")}
            if tree is not None:
                tree.terminate(3)
        finally:
            if tree is not None:
                tree.close()
                with self._lock:
                    self._running_procs.pop(job_id, None)
            with self._lock:
                # 先记「未上报」再移出 active：两者之间的心跳不能漏报这条任务，
                # 否则 hub 会按「心跳里没有」把它回收成 error。
                self._unreported.add(job_id)
                self._active_jobs.discard(job_id)
                self._cancel_flags.pop(job_id, None)
            try:
                (run_dir / ".running").unlink()
            except OSError:
                pass
            self._finish(job_id, outcome)
            self._schedule_cleanup(run_dir, outcome["status"])

    # ------------------------------------------------------------ 准备阶段
    def _mirror_for(self, project: str) -> Path:
        """镜像路径必须在 mirror_root 之内（I4）。"""
        if not valid_project_name(project):
            raise RuntimeError(f"invalid project name: {project!r}")
        root = self.mirror_root.resolve()
        path = (root / f"{project}.git").resolve()
        if not path.is_relative_to(root):
            raise RuntimeError(f"mirror path escapes mirror_root: {path}")
        return path

    def _fetch_git_relay(self, job_id: str, mirror: Path, sha: str,
                         deadline: float) -> bool:
        """``CALLTABLE_GIT_URL`` + ``CALLTABLE_GIT_REFS``：从远端仓库取临时 ref。

        返回任务提交是否已在镜像里。任何失败只记一行日志、返回 False，
        调用方照旧走 hub bundle。token 只经环境变量形式的 git 配置
        （``GIT_CONFIG_COUNT``）传给 git：不进 argv、不写镜像配置、不进日志。
        """
        url = os.environ.get("CALLTABLE_GIT_URL", "").strip()
        refs = os.environ.get("CALLTABLE_GIT_REFS", "").strip().rstrip("/")
        if not url or not refs:
            return False
        token = os.environ.get("CALLTABLE_GIT_TOKEN", "").strip()
        secrets = [token] if token else []
        try:
            if not _GIT_RELAY_REFS_RE.fullmatch(refs):
                raise RuntimeError(f"bad CALLTABLE_GIT_REFS {refs!r}")
            if url.startswith("-"):
                raise RuntimeError("bad CALLTABLE_GIT_URL")
            budget = min(GIT_RELAY_FETCH_TIMEOUT, (deadline - time.time()) / 2)
            if budget < GIT_RELAY_MIN_TIMEOUT:
                print(f"[exec] {job_id}: git relay skipped: not enough time left; "
                      "using hub bundles", flush=True)
                return False
            env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
            if token:
                basic = base64.b64encode(
                    f"x-access-token:{token}".encode("utf-8")).decode("ascii")
                secrets.append(basic)
                index = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
                env.update({"GIT_CONFIG_COUNT": str(index + 1),
                            f"GIT_CONFIG_KEY_{index}": "http.extraheader",
                            f"GIT_CONFIG_VALUE_{index}":
                                f"Authorization: Basic {basic}"})
            gitops.ensure_mirror(mirror)
            started = time.time()
            gitops.git(["-C", str(mirror), "fetch", "--no-tags", "--quiet", url,
                        f"+{refs}/*:refs/calltable/relay/*"],
                       env=env, input_data="",
                       timeout=budget)
            if not gitops.has_commit(mirror, sha):
                print(f"[exec] {job_id}: git relay: commit {sha[:12]} not in "
                      f"{refs}; falling back to hub bundles", flush=True)
                return False
            print(f"[exec] {job_id}: git relay: fetched {refs} from {url} in "
                  f"{time.time() - started:.1f}s", flush=True)
            return True
        except Exception as exc:
            message = " ".join(str(exc).split())
            for secret in secrets:
                message = message.replace(secret, "***")
            print(f"[exec] {job_id}: git relay fetch failed, falling back to hub "
                  f"bundles: {type(exc).__name__}: {message[:500]}", flush=True)
            return False

    def _refresh_cancel(self, job_id: str) -> None:
        """向 hub 查一次这条任务是否已请求取消（查不到就沿用已知状态）。

        这一步只是提前发现取消，hub 出任何问题（断开连接、超时、返回的
        不是 JSON）都不能让没人取消的任务因此失败：照常拉起进程，之后的
        取消交给轮询线程。超时取短，慢吞吞的 hub 不能把拉起拖一分钟。
        """
        if self._cancel_flags.get(job_id):
            return
        try:
            state = self.client.get_json(f"/api/jobs/{job_id}", timeout=10)
        except (HubError, OSError, ValueError):
            return
        if isinstance(state, dict) and state.get("cancel_requested"):
            self._cancel_flags[job_id] = True

    def _checkpoint(self, job_id: str, deadline: float, stage: str = "") -> None:
        if self._cancel_flags.get(job_id):
            raise Aborted("cancelled")
        # 用 >=：预算为 0（timeout=0）时必须立刻中止，不能靠时钟再走一格里。
        if time.time() >= deadline:
            raise Aborted("timeout")

    def _supervise(self, job: dict, tree, job_id: str) -> tuple[str, str]:
        timeout = int(job["timeout"]) if job.get("timeout") is not None else 3600
        started = job.get("started")
        if isinstance(started, (int, float)) and started > 0:
            # hub 记录的接收时间是预算起点；检出/安装已经花掉的时间要扣掉。
            timeout = max(0, timeout - max(0, time.time() - float(started)))
        deadline = time.time() + timeout
        status = "succeeded"
        error = ""
        while True:
            code = tree.poll()
            if code is not None:
                break
            if getattr(tree, "soft_limit_exceeded", False):
                # direct 模式没有 cgroup：采样连续两次超限后执行器自己动手。
                status = "error"
                error = "memory limit exceeded (soft, direct mode)"
                tree.terminate(5)
                break
            if time.time() >= deadline or self._cancel_flags.get(job_id):
                status = "cancelled" if self._cancel_flags.get(job_id) else "timeout"
                tree.terminate(5)
                break
            time.sleep(0.4)
        if tree.poll() is None:
            tree.terminate(5)
        if status == "succeeded" and tree.poll() not in (0, None):
            status = "failed"
        return status, error

    def _sample_loop(self, job_id: str, tree) -> None:
        interval = self.config.sample_interval
        while tree.poll() is None and not self._stop.is_set():
            sample = tree.sample()
            try:
                self.client.post_json(f"/api/jobs/{job_id}/samples", {
                    "samples": [{
                        "cpu_usec": sample.cpu_usec,
                        "mem_current": sample.mem_current,
                        "mem_peak": sample.mem_peak,
                    }]})
            except HubError:
                pass
            time.sleep(interval)

    # ------------------------------------------------------------ 终态上报
    def _pending_path(self, job_id: str) -> Path:
        return self.pending_dir / f"{job_id}.json"

    def _has_unsent_results(self) -> bool:
        """本地还有没有没送达的终态（盘上的 pending，或写不进盘的内存副本）。"""
        with self._lock:
            if self._unpersisted:
                return True
        try:
            return any(self.pending_dir.glob("*.json"))
        except OSError:
            return False

    def _result_pending(self, job_id: str) -> bool:
        """这份终态还在本地（盘上的 pending 文件，或写不进盘时的内存副本）。"""
        with self._lock:
            if job_id in self._unpersisted:
                return True
        return self._pending_path(job_id).exists()

    def _pending_result(self, job_id: str) -> dict | None:
        """读一份还没送达的终态；盘上的优先，其次内存副本。"""
        with self._lock:
            outcome = self._unpersisted.get(job_id)
        if isinstance(outcome, dict):
            return outcome
        try:
            outcome = json.loads(self._pending_path(job_id).read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return outcome if isinstance(outcome, dict) else None

    def _persist_pending(self, job_id: str, outcome: dict) -> bool:
        """上报之前先把终态原子写进 ``pending/<job_id>.json``。

        先写 ``.<job_id>.json.tmp``、fsync、再 rename，最后刷目录项：崩溃或
        断电时盘上要么没有这份文件、要么是完整的一份，不会有半截 JSON。
        写不进去（磁盘满、只读、序列化失败）时结果退到内存副本，照样按心跳
        重放，只是执行器退出就没了——日志里写明这一点。
        """
        target = self._pending_path(job_id)
        partial = self.pending_dir / f".{job_id}.json{PENDING_TMP_SUFFIX}"
        try:
            payload = json.dumps(outcome, ensure_ascii=False)
            with open(partial, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, target)
            _fsync_dir(self.pending_dir)
        except (OSError, TypeError, ValueError) as exc:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                self._unpersisted[job_id] = outcome
            print(f"[exec] cannot persist pending finish for {job_id} ({exc}); "
                  "the result is kept in memory only and lost if the executor "
                  "exits", file=sys.stderr, flush=True)
            return False
        with self._lock:
            self._unpersisted.pop(job_id, None)
        return True

    def _settle(self, job_id: str) -> None:
        """这份终态已了结：删 pending、清内存副本与退避记录，心跳不再报告它。"""
        try:
            self._pending_path(job_id).unlink(missing_ok=True)
        except OSError as exc:
            print(f"[exec] cannot remove pending finish for {job_id}: {exc}",
                  file=sys.stderr, flush=True)
        with self._lock:
            self._unpersisted.pop(job_id, None)
            self._replay_after.pop(job_id, None)
            self._unreported.discard(job_id)

    def _deliver(self, job_id: str, outcome: dict,
                 timeout: float | None = None) -> str | None:
        """把一份终态送一次（任务线程和心跳前的重放共用一个入口）。

        返回 ``"reported"``（hub 收下了）、``"settled"``（这份结果已经了结，
        不必再送）或 ``None``（另一个线程正在送它）。没送到时抛 HubError，
        结果原样留在 pending 里。同一个任务同时只有一个线程在送，重放和任务
        线程不会把同一份结果发两遍。``timeout`` 是单次请求的超时：重放用它把
        一整轮限在预算内，免得一个卡住的请求把心跳拖过重放预算。
        """
        with self._lock:
            if job_id in self._delivering:
                return None
            if job_id not in self._unpersisted and not self._pending_path(
                    job_id).exists():
                return "settled"
            self._delivering.add(job_id)
        try:
            try:
                if timeout is None:
                    self.client.post_json(
                        f"/api/exec/jobs/{job_id}/finish", outcome)
                else:
                    self.client.post_json(
                        f"/api/exec/jobs/{job_id}/finish", outcome,
                        timeout=timeout)
            except HubError as exc:
                if _finish_rejected(exc):
                    # hub 明确拒收（M1 的 finish_rejected）：重试没有别的结果。
                    self._settle(job_id)
                    return "settled"
                raise
            self._settle(job_id)
            return "reported"
        finally:
            with self._lock:
                self._delivering.discard(job_id)

    def _finish(self, job_id: str, outcome: dict) -> bool:
        """终态先原子落盘，再指数退避重试上报（最长 10 分钟）。

        落盘在最前面：任务线程、心跳前的重放、以及重启后的下一个执行器都能
        接着送，短暂错误（连不上、超时、5xx）不丢结果。

        先登记 ``_unreported`` 再落盘：落盘会先把这份结果暴露给心跳前的重放，
        那个线程可能会抢先送达并在 :meth:`_settle` 里把它从 ``_unreported``
        摘掉。顺序反过来的话，这里会在送达之后又把它登记回去，心跳就会永远
        报一条早就结束的任务。
        """
        with self._lock:
            self._unreported.add(job_id)
        persisted = self._persist_pending(job_id, outcome)
        delay = 1.0
        started = time.time()
        while True:
            try:
                result = self._deliver(job_id, outcome)
            except HubError as exc:
                elapsed = time.time() - started
                if elapsed >= FINISH_BUDGET:
                    where = ("pending" if persisted else
                             "memory only (lost if the executor exits)")
                    print(f"[exec] finish for {job_id} failed after "
                          f"{int(elapsed)}s; queued for replay from {where}: "
                          f"{exc}", file=sys.stderr, flush=True)
                    return False
                print(f"[exec] finish for {job_id} failed ({exc}); "
                      f"retrying in {delay:.0f}s", file=sys.stderr, flush=True)
                time.sleep(min(delay, FINISH_MAX_DELAY))
                delay = min(delay * 2, FINISH_MAX_DELAY)
                continue
            if result is not None:
                return True
            # 另一个线程（心跳前的重放）正在送同一份结果：等它，别再发一遍。
            if not self._result_pending(job_id):
                return True
            if time.time() - started >= FINISH_BUDGET:
                return False
            time.sleep(0.2)

    # ------------------------------------------------------------ 终态重放
    def _recover_partial_pending_once(self) -> None:
        """启动时补完崩溃在 fsync 与 rename 之间的临时文件（只做一次）。

        只能在任何任务线程开始写 pending 之前跑：正在写入的 ``.tmp`` 既不完整
        也不能改名，混进重放会被挪成 ``.bad`` 或被提前 rename 掉。"""
        with self._lock:
            if self._pending_recovered:
                return
            self._pending_recovered = True
        self._recover_partial_pending()

    def _recover_partial_pending(self) -> None:
        """补完崩溃在 fsync 与 rename 之间的那份临时文件，清掉没用的残留。"""
        try:
            partials = sorted(self.pending_dir.glob(
                f".*.json{PENDING_TMP_SUFFIX}"))
        except OSError:
            return
        for partial in partials:
            job_id = partial.name[1:-len(".json" + PENDING_TMP_SUFFIX)]
            if not job_id:
                self._set_aside(partial, "bad temporary file name")
                continue
            target = self._pending_path(job_id)
            if target.exists():
                # 正式的 pending 已经在（写新的时旧的还没删）：临时文件没用。
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            try:
                json.loads(partial.read_text(encoding="utf-8"))
                os.replace(partial, target)
            except (OSError, ValueError) as exc:
                self._set_aside(partial, f"unusable temporary file: {exc}")

    def _set_aside(self, path: Path, reason: str) -> None:
        """把读不了的 pending 改名成 ``*.bad``：留着查原因，但不再每轮重试。"""
        try:
            path.replace(path.with_name(path.name + ".bad"))
        except OSError:
            return
        print(f"[exec] pending {path.name} moved aside ({reason})",
              file=sys.stderr, flush=True)

    def _replay_wait(self, job_id: str) -> float:
        """这份结果还要等多久才能再试（只有 hub 明确答复过的失败才退避）。"""
        with self._lock:
            entry = self._replay_after.get(job_id)
        if not entry:
            return 0.0
        return max(0.0, entry[0] - time.monotonic())

    def _back_off(self, job_id: str) -> None:
        with self._lock:
            previous = self._replay_after.get(job_id, (0.0, 0.0))[1]
        delay = min(REPLAY_BACKOFF_MAX,
                    previous * 2 if previous else REPLAY_BACKOFF)
        with self._lock:
            self._replay_after[job_id] = (time.monotonic() + delay, delay)

    def _snapshot_inherited_results(self) -> set[str]:
        """记下启动时还没送出去的终态：这些是上一个执行器实例留下的任务。

        它们在 hub 上还绑着旧 worker。本实例的第一次心跳会按
        ``executor worker changed`` 无条件把它们结算成 error（hub 的
        ``Store.heartbeat`` 先判 worker，之后才看 ``running``），所以只把任务
        报进 ``running`` 保护不了，必须推迟第一次心跳。
        """
        try:
            paths = sorted(self.pending_dir.glob("*.json"))
        except OSError:
            paths = []
        with self._lock:
            self._inherited = ({path.stem for path in paths}
                               | set(self._unpersisted))
            return set(self._inherited)

    def _undelivered_inherited(self) -> set[str]:
        """继承的结果里还有哪些没送到（盘上的 pending 或内存副本）。"""
        with self._lock:
            inherited = set(self._inherited)
            memory = set(self._unpersisted)
        if not inherited:
            return set()
        present: set[str] = set()
        try:
            for path in self.pending_dir.glob("*.json"):
                present.add(path.stem)
        except OSError:
            pass
        return inherited & (memory | present)

    def _await_inherited_results(self, grace: float | None = None) -> bool:
        """有界推迟第一次心跳/领任务，先把上一个实例留下的结果送出去。

        只对启动时继承的结果生效：本实例自己任务的终态不需要推迟心跳（它们
        还在 ``running`` 里）。窗口默认 :data:`INHERITED_RESULT_GRACE`，到点照常
        心跳——那时 hub 可能已经把旧任务回收成 error，真实结果只剩一条说明，
        但主机不能因此一直不心跳、不领任务。一次性执行器（Cloud Run 每次都是
        全新容器，没有上一个实例）不推迟，避免拖住 attach。
        返回是否全部送达。
        """
        pending = self._undelivered_inherited()
        if not pending or self.once:
            return not pending
        window = float(INHERITED_RESULT_GRACE if grace is None else grace)
        deadline = time.monotonic() + max(0.0, window)
        print(f"[exec] holding the first heartbeat until {len(pending)} result(s) "
              f"left by the previous executor are delivered (at most "
              f"{window:.0f}s)", flush=True)
        while not self._stop.is_set():
            if not self._undelivered_inherited():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._replay_pending_quietly(budget=min(REPLAY_BUDGET, remaining))
            if not self._undelivered_inherited():
                return True
            self._stop.wait(min(INHERITED_HOLD_PAUSE,
                                max(0.0, deadline - time.monotonic())))
        left = self._undelivered_inherited()
        if left:
            print(f"[exec] {len(left)} inherited result(s) still undelivered "
                  f"after {window:.0f}s; heartbeating anyway (the hub may reap "
                  f"those jobs as worker changed)", file=sys.stderr, flush=True)
        return not left

    def _replay_pending_quietly(self, budget: float | None = None) -> int:
        """重放一次，但绝不让重放把心跳、启动或退出弄挂。"""
        try:
            return self.replay_pending(budget=budget)
        except Exception as exc:  # pragma: no cover - 兜底：心跳/退出优先
            print(f"[exec] pending replay: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            return 0

    def replay_pending(self, budget: float | None = None) -> int:
        """重放没送达的终态（心跳前、janitor、启动时各调一次）。

        有界：整轮不超过 ``budget`` 秒（默认 :data:`REPLAY_BUDGET`，单次请求也
        按剩余预算设超时，一个卡住的请求不能把心跳拖过预算）、最多真正试
        :data:`REPLAY_MAX_FILES` 份、最多看 :data:`REPLAY_MAX_WALK` 个候选。
        hub 这会儿用不了（连不上、超时、5xx）就立刻收手；hub 明确答复的失败按
        任务退避，且退避中的结果只是本轮跳过、不占尝试名额。候选从上一轮停下
        的位置轮转，所以积压的结果不会被前面那些退避中的结果永远挡住。
        """
        budget = REPLAY_BUDGET if budget is None else max(0.0, float(budget))
        deadline = time.monotonic() + budget
        # 这里**不**碰 ``.tmp``：恢复只在启动时做一次，活跃写入的临时文件
        # 不能被重放挪走或提前 rename。
        try:
            paths = sorted(self.pending_dir.glob("*.json"))
        except OSError:
            paths = []
        with self._lock:
            memory = dict(self._unpersisted)
        candidates: list[tuple[str, Path | None]] = [(p.stem, p) for p in paths]
        candidates += [(job_id, None) for job_id in memory]
        total = len(candidates)
        if not total:
            return 0
        with self._lock:
            start = self._replay_cursor % total
            self._replay_cursor = (start + 1) % total   # 至少前进一格
        rotated = candidates[start:] + candidates[:start]
        replayed = 0
        attempts = 0
        walked = 0
        for walked, (job_id, path) in enumerate(rotated[:REPLAY_MAX_WALK], start=1):
            if attempts >= REPLAY_MAX_FILES:
                break
            if time.monotonic() >= deadline:
                print(f"[exec] pending replay used its {budget:.0f}s budget; "
                      "the rest is left for the next heartbeat",
                      file=sys.stderr, flush=True)
                break
            if self._replay_wait(job_id) > 0:
                continue
            outcome = memory.get(job_id) if path is None else self._read_pending(path)
            if not isinstance(outcome, dict):
                continue
            attempts += 1
            remaining = deadline - time.monotonic()
            try:
                # 单次请求也不能超预算：预算不够时最多再多给 1 秒，而不是用
                # 客户端的默认 60 秒把心跳拖死。
                result = self._deliver(job_id, outcome,
                                       timeout=max(1.0, remaining))
            except HubError as exc:
                if _hub_unavailable(exc):
                    print(f"[exec] pending finish still failing for {job_id}: "
                          f"{exc}", file=sys.stderr, flush=True)
                    break
                self._back_off(job_id)
                print(f"[exec] pending finish for {job_id} was refused: {exc}; "
                      "retrying later", file=sys.stderr, flush=True)
                continue
            if result == "reported":
                replayed += 1
                print(f"[exec] replayed pending finish for {job_id}", flush=True)
        with self._lock:
            self._replay_cursor = (start + walked) % max(1, total)
        return replayed

    def _read_pending(self, path: Path) -> dict | None:
        """读一份 pending；读不了的挪成 ``*.bad``（留着查原因，不再每轮重试）。"""
        try:
            outcome = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            self._set_aside(path, f"unreadable pending finish: {exc}")
            return None
        except OSError as exc:
            print(f"[exec] cannot read pending finish {path.name}: {exc}",
                  file=sys.stderr, flush=True)
            return None
        return outcome if isinstance(outcome, dict) else None

    # ------------------------------------------------------------ 环境与脚本
    def _write_launcher(self, run_dir: Path, job: dict, profile: dict,
                        work: Path) -> Path:
        shell = self.config.shell
        if shell.lower().endswith("cmd.exe"):
            raise RuntimeError(
                "cmd.exe launcher is not supported on Windows; install Git Bash "
                "and set config.shell to bash.exe (see README)")
        env_files = [str(x) for x in (profile.get("env_files") or [])]
        command = shlex.join([str(x) for x in job.get("cmd") or []])
        script = run_dir / "run.sh"
        lines = [
            "set -e",
            # 任务里所有产物都必须是 owner-only：LocalStorage 会拒绝组可写的
            # 目录链。发行版默认 umask 0002 时会放开，这里显式收紧。
            "umask 077",
            f'cd {shlex.quote(str(work))}',
        ]
        score = self.config.task_oom_score_adj
        if score:
            # 让 OOM killer 优先选任务而不是执行器。只有该文件存在且可写时
            # 才写：Windows / Git Bash 上没有 procfs，而 `echo > 文件 2>/dev/null`
            # 挡不住重定向失败本身的报错（bash 从左到右处理重定向，打开目标
            # 失败时 stderr 还没被重定向），日志里会每任务刷一条
            # "No such file or directory"。Linux 上该文件对自己永远可写，
            # 行为不变。
            lines.append(f"if [ -w {shlex.quote(OOM_SCORE_ADJ_PATH)} ]; then "
                         f"echo {int(score)} > "
                         f"{shlex.quote(OOM_SCORE_ADJ_PATH)}; fi")
        for path in env_files:
            quoted = shlex.quote(path)
            lines.append(f'if [ ! -r {quoted} ]; then echo "calltable: env file '
                         f'not readable: {path}" >&2; exit 97; fi')
            lines.append(f'. {quoted}')
        for setup in (profile.get("setup") or []):
            # 用子 shell 隔离，setup 里的 cd 不能影响真正的任务命令。
            lines.append("( %s )" % setup)
        lines.append(command)
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return script

    def _argv(self, script: Path) -> list[str]:
        return [self.config.shell, str(script)]

    def _build_env(self, job: dict, profile: dict, tmp: Path,
                   work: Path) -> dict[str, str]:
        env = os.environ.copy()
        # 任务代码和它打的日志不该读到 hub 的 bearer token。只动这份副本：
        # 执行器自己仍从 os.environ 取 token（config.token_value）。profile 的
        # env 在这之后合并，所以仍然可以显式设置同名变量。
        for name in HUB_CREDENTIAL_ENV:
            env.pop(name, None)
        env.update({str(k): str(v) for k, v in (profile.get("env") or {}).items()})
        env["TMPDIR"] = str(tmp)
        env["TMP"] = str(tmp)
        env["TEMP"] = str(tmp)
        env["CALLTABLE_JOB_ID"] = job["id"]
        env["CALLTABLE_PROJECT"] = job["project"]
        env["CALLTABLE_HOST"] = job["host"]
        env["CALLTABLE_WORKTREE"] = str(work)
        env["CALLTABLE_SHA"] = job.get("sha") or ""
        env["CALLTABLE_BASE_SHA"] = (job.get("base_sha") or job.get("sha")
                              or job.get("base_ref_sha") or "")
        env["CALLTABLE_DIRTY"] = "1" if job.get("dirty") else "0"
        env["CALLTABLE_BASE_REF_SHA"] = job.get("base_ref_sha") or ""
        env["CALLTABLE_SUBMITTER"] = job.get("submitter") or ""
        # 构建/工具链默认值：任务结束不留常驻 MSBuild / VBCSCompiler / 遥测进程。
        env["MSBUILDDISABLENODEREUSE"] = "1"
        env["DOTNET_CLI_USE_MSBUILD_SERVER"] = "0"
        env["UseSharedCompilation"] = "false"
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        env["DOTNET_NOLOGO"] = "1"
        env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        env.pop("GIT_INDEX_FILE", None)
        return env

    # ------------------------------------------------------------ 产物
    def _collect_artifacts(self, job_id: str, work: Path,
                           patterns: list[str]) -> list[str]:
        """收集产物；任何失败只记一条说明，绝不改 status / exit_code（B3）。

        不能依赖 ``Path.glob("**/...")``：它会遍历 node_modules 等巨型目录，
        实测在 Windows 上超过 10 分钟，导致主进程已退出而 job 仍显示 running。
        这里用 os.walk 剪枝 + 总时限，保证任务结束不被产物收集拖住。
        """
        notes: list[str] = []
        seen: set[str] = set()
        count = 0
        root = work.resolve()
        regexes = []
        for pattern in patterns:
            if ".." in Path(pattern).parts:
                notes.append(f"artifact pattern rejected: {pattern}")
                continue
            regexes.append((pattern, _artifact_regex(pattern)))
        if not regexes:
            return notes
        deadline = time.monotonic() + ARTIFACT_SCAN_SECONDS
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in ARTIFACT_SKIP_DIRS
                and not (Path(dirpath) / name).is_symlink())
            for name in sorted(filenames):
                # 用 >=：时间预算 0 表示「不允许扫描」，必须立即计为截断（
                # Windows 上 time.monotonic() 精度 ~15.6ms，用 > 会漏判）。
                if time.monotonic() >= deadline:
                    truncated = True
                    break
                path = Path(dirpath) / name
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                matched = next(
                    (pattern for pattern, regex in regexes if regex.fullmatch(rel)),
                    None)
                if matched is None or rel in seen:
                    continue
                seen.add(rel)
                count += 1
                if count > MAX_ARTIFACTS:
                    notes.append(f"artifact limit reached ({MAX_ARTIFACTS})")
                    return notes
                try:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(root):
                        notes.append(f"artifact outside worktree skipped: {path}")
                        continue
                    if resolved.stat().st_size > MAX_ARTIFACT_BYTES:
                        notes.append(f"artifact too large skipped: {rel}")
                        continue
                    self.client.upload_file(
                        f"/api/jobs/{job_id}/artifacts", resolved,
                        headers={"X-Calltable-Path": urllib.parse.quote(rel)})
                except Exception as exc:
                    notes.append(f"artifact upload failed: {name}: "
                                 f"{type(exc).__name__}: {exc}")
            if truncated:
                break
        if truncated:
            notes.append(f"artifact scan truncated after "
                         f"{ARTIFACT_SCAN_SECONDS:.0f}s")
        return notes

    # ------------------------------------------------------------ 清理
    def _schedule_cleanup(self, run_dir: Path, status: str) -> None:
        retention = (self.config.retention_success if status == "succeeded"
                     else self.config.retention_failure)
        finished = now()
        marker = run_dir / ".retention"
        try:
            marker.write_text(str(finished + retention), encoding="utf-8")
        except OSError:
            pass
        # 磁盘水位保护按「完成时间从旧到新」清理，需要一个明确的完成时间。
        try:
            (run_dir / disk.FINISHED_MARKER).write_text(str(finished),
                                                        encoding="utf-8")
        except OSError:
            pass

    def janitor(self) -> None:
        """清掉过期 run 目录；正在跑的（.running）不碰，最后统一 worktree prune。"""
        cutoff = now()
        removed = 0
        for entry in self.runs_root.iterdir():
            if not entry.is_dir():
                continue
            marker = entry / ".running"
            if marker.exists():
                owner = ""
                try:
                    owner = marker.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                # 只有「本执行器当前在跑」的任务才受保护；上次崩溃留下的
                # .running 目录要按普通残留处理，否则永远清不掉。
                if not owner or owner == self.worker_id:
                    continue
            marker = entry / ".retention"
            try:
                deadline = float(marker.read_text().strip())
            except (OSError, ValueError):
                try:
                    age = cutoff - entry.stat().st_mtime
                except OSError:
                    continue
                if age < self.config.retention_failure:
                    continue
                deadline = 0
            if deadline > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        # 旧版本用 worktree 建检出；每轮 prune 一次，清掉历史遗留登记（I5）。
        for mirror in self.mirror_root.glob("*.git"):
            gitops.git(["-C", str(mirror), "worktree", "prune"], check=False)


class LogPump:
    """把进程输出按块上传：读线程 + 定时 flush 线程。"""

    def __init__(self, client: HubClient, job_id: str, stream):
        self.client = client
        self.job_id = job_id
        self.stream = stream
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.writer = threading.Thread(target=self._write, daemon=True)

    def start(self) -> None:
        self.reader.start()
        self.writer.start()

    def _read(self) -> None:
        try:
            while True:
                chunk = self.stream.read1(8192) if hasattr(self.stream, "read1") \
                    else self.stream.read(8192)
                if not chunk:
                    break
                with self.lock:
                    self.buffer.extend(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.stop_event.set()

    def _write(self) -> None:
        while True:
            finished = self.stop_event.is_set()
            with self.lock:
                data = bytes(self.buffer[:LOG_CHUNK])
                del self.buffer[:len(data)]
            if data:
                self._post(data)
            elif finished:
                return
            if not data:
                time.sleep(LOG_FLUSH_SECONDS)

    def _post(self, data: bytes) -> None:
        for attempt in range(3):
            try:
                self.client.request("POST", f"/api/jobs/{self.job_id}/logs",
                                    data=data,
                                    headers={"Content-Type": "application/octet-stream"})
                return
            except HubError as exc:
                if attempt == 2:
                    print(f"[exec] log upload for {self.job_id} failed: {exc}",
                          file=sys.stderr, flush=True)
                else:
                    time.sleep(0.5 * (attempt + 1))

    def finish(self) -> None:
        self.reader.join(timeout=15)
        self.stop_event.set()
        self.writer.join(timeout=20)
        with self.lock:
            leftover = bytes(self.buffer)
            self.buffer.clear()
        if leftover:
            self._post(leftover)


def serve(config: Config, *, once: bool = False,
          job_id: str | None = None) -> int:
    # 容器里没有 init 时执行器就是 PID 1（Cloud Run 镜像）：先拆出一个只管
    # 回收孤儿进程的 init，否则任务留下的后台进程会变成僵尸越积越多。
    # 必须在起任何线程之前做；不是 PID 1 时什么都不发生。
    proc_init.split_if_pid1()
    executor = Executor(config, once=once, job_id=job_id)
    try:
        executor.run()
    except KeyboardInterrupt:
        executor.stop()
    return executor.exit_code
