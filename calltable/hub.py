"""hub：任务队列、主机、锁、日志、采样与产物的权威服务。

默认只监听 127.0.0.1（配置里显式写内网地址也可以）。
所有 /api/* 都需要 ``Authorization: Bearer <token>``；唯一例外是看板的产物
下载（``GET /api/jobs/<id>/artifacts/<name>?token=``），因为 <a download> 带不了
请求头；看板本身改用 fetch + header 下载，保留 query 入口只为兼容旧链接。

调度原子性见 :func:`calltable.db.Store.claim`；执行器对账见
:func:`calltable.db.Store.heartbeat`。
"""

from __future__ import annotations

import hashlib
import json
import re
import select
import socket
import socketserver
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import RULES_CLIENT_VERSION, __version__, gitops
from .config import Config
from .dashboard import render_page
from .db import QUEUED, TERMINAL, IdempotencyConflict, Store
from .profiles import (Profile, find_profile, load_profiles, match_rule,
                       resolve_job)
from .util import (MAX_ARTIFACT_PATTERNS, artifact_pattern_problem, iso,
                   json_dumps, now, parse_duration, valid_project_name,
                   version_tuple)
from .webhooks import (WebhookDispatcher, deliveries_for_display,
                       validate_url)

MAX_JSON = 8 * 1024 * 1024
MAX_BUNDLE = 1024 * 1024 * 1024
MAX_ARTIFACT = 512 * 1024 * 1024
IO_BLOCK = 1024 * 1024
UPLOAD_SLOTS = 2
#: 单任务最多几个 webhook（防止误提交一个巨大的列表）。
MAX_JOB_WEBHOOKS = 10
#: 幂等 key 的合法字符与长度。
IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
#: 0.3 新增能力的自述（客户端据此判断是否降级）。
CAPABILITIES = ["queue_eta", "wait_longpoll", "events_sse", "webhooks",
                "idempotency", "queue_hold", "exec_attach", "cloudrun"]


class Hub:
    def __init__(self, config: Config, store: Store | None = None, *,
                 cloudrun_api=None):
        self.config = config
        self.data_dir = config.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bundles = self.data_dir / "bundles"
        self.artifact_dir = self.data_dir / "artifacts"
        self.mirror_dir = self.data_dir / "mirrors"
        self.upload_dir = self.data_dir / "uploads"
        for path in (self.bundles, self.artifact_dir, self.mirror_dir,
                     self.upload_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.store = store or Store(self.data_dir / "hub.sqlite3",
                                    log_cap=config.log_cap_bytes,
                                    estimate_window=config.estimate_window,
                                    estimate_min_samples=config.estimate_min_samples)
        # 预计时间 / webhook / 幂等 / 心跳的 hub 级配置。
        self.store.estimate_window = config.estimate_window
        self.store.estimate_min_samples = config.estimate_min_samples
        self.store.webhook_urls = (list(config.webhook_urls)
                                   if config.webhook_secret else [])
        self.store.dashboard_base_url = config.public_url
        self.store.idempotency_window = config.idempotency_window_seconds
        self.profiles = load_profiles(config.profiles_dir)
        self.upload_slots = threading.BoundedSemaphore(UPLOAD_SLOTS)
        self.wait_slots = threading.BoundedSemaphore(config.max_waiters)
        self.event_slots = threading.BoundedSemaphore(config.max_event_streams)
        self._thick_locks: dict[str, threading.Lock] = {}
        self._thick_guard = threading.Lock()
        self._stop = threading.Event()
        self._janitor: threading.Thread | None = None
        self._cloudrun_thread: threading.Thread | None = None
        self._webhooks: WebhookDispatcher | None = None
        self._warned: set[str] = set()
        self.keep_ref_seconds = int(config.raw.get("keep_ref_days", 14) * 86400)

        # 配置里的主机先落库；seed_host 不会刷新 last_seen，否则会掩盖失联。
        for name, spec in config.hosts.items():
            self.store.seed_host(name, spec.slots, spec.labels, spec.note)
        for group, members in config.groups.items():
            for member in members:
                if member not in config.hosts:
                    self._warn(f"group {group} lists {member}, but it is not in "
                               "hosts; it will never receive jobs")
            providers = [member for member in members
                         if member in config.hosts
                         and config.hosts[member].provider == "cloudrun"]
            if len(providers) > 1:
                self._warn(f"group {group} lists several cloudrun hosts "
                           f"({', '.join(providers)}); a job keeps only the first "
                           "one its project has a profile for")
        # hub 重启：遗留 running 任务进入「待对账」，由心跳或超时回收。
        self.store.mark_running_for_reconcile()
        # 按需拉起的 provider 主机（Cloud Run）。没有配置就完全不加载。
        self.cloudrun = None
        if any(spec.provider == "cloudrun" for spec in config.hosts.values()):
            from .cloudrun import Provisioner
            self.cloudrun = Provisioner(self, api=cloudrun_api)
        for name, spec in config.hosts.items():
            if spec.provider not in ("static", "cloudrun"):
                self._warn(f"host {name} has unknown provider {spec.provider!r}; "
                           "it is treated as a static host")
        if self.config.webhook_urls and not self.config.webhook_secret:
            self._warn("webhook_urls is configured but webhook_secret is empty; "
                       "webhooks are disabled until a signing secret is set")

    # ------------------------------------------------------------ 生命周期
    def start_janitor(self) -> None:
        if self._janitor is None:
            self._janitor = threading.Thread(target=self._janitor_loop,
                                             daemon=True, name="hub-janitor")
            self._janitor.start()
        self.start_cloudrun()
        self.start_webhooks()

    def start_cloudrun(self) -> None:
        """Cloud Run 的拉起 / 对账单独一个线程，不放在 janitor 里。

        每次 gcloud 调用最长等 120s，而且是串行的（每个活跃 execution 一次
        describe、每个排队任务一次 execute）。gcloud 变慢或卡住时只拖住这个
        线程，常驻主机的失联回收、排队超时和保留期清理照常按 janitor 的节奏跑。
        没有 cloudrun 主机就不起线程。
        """
        if self.cloudrun is None or self._cloudrun_thread is not None:
            return
        self._cloudrun_thread = threading.Thread(
            target=self._cloudrun_loop, daemon=True, name="hub-cloudrun")
        self._cloudrun_thread.start()

    def start_webhooks(self) -> None:
        """后台投递线程：不阻塞调度；单独抽出来方便测试直接调 run_once。"""
        if self._webhooks is not None:
            return
        if not self.config.webhook_secret:
            return
        self._webhooks = WebhookDispatcher(self.store, self.config, self._stop)
        self._webhooks.start()

    def start_upstreams(self) -> None:
        """按配置后台拉取只读上游的 main，让客户端增量只剩自己的改动。"""
        for project, url in self.config.upstreams.items():
            if not valid_project_name(project):
                self._warn(f"upstream {project}: invalid project name")
                continue
            threading.Thread(target=self._upstream_loop, args=(project, url),
                             daemon=True,
                             name=f"hub-upstream-{project}").start()

    def upstream_once(self, project: str, url: str | None = None) -> str:
        if not valid_project_name(project):
            raise ValueError(f"invalid project name: {project!r}")
        url = url or self.config.upstreams.get(project)
        if not url:
            raise ValueError(f"no upstream configured for {project}")
        mirror = self.mirror_for(project)
        gitops.git(["-C", str(mirror), "fetch", "--prune", "--no-tags", url,
                    "+refs/heads/main:refs/calltable/upstream/main"], timeout=180)
        out = gitops.git(["-C", str(mirror), "rev-parse",
                          "refs/calltable/upstream/main"], timeout=30, check=False)
        return out.stdout.strip()

    def _upstream_loop(self, project: str, url: str) -> None:
        interval = self.config.upstream_interval
        failures = 0
        while not self._stop.is_set():
            try:
                sha = self.upstream_once(project, url)
                print(f"[hub] upstream {project}: {sha[:12] if sha else '?'}",
                      flush=True)
                failures = 0
                delay = interval
            except Exception as exc:  # 网络失败：指数退避，最长 1h
                failures += 1
                delay = min(interval * (2 ** min(failures, 5)), 3600.0)
                print(f"[hub] upstream {project} fetch failed "
                      f"(attempt {failures}, retry in {delay:.0f}s): {exc}",
                      flush=True)
            if self._stop.wait(delay):
                return

    def _janitor_loop(self) -> None:
        # 首次清理放到后台线程里跑，不阻塞 hub 启动（A12）。
        time.sleep(min(2.0, self.config.janitor_interval))
        while not self._stop.wait(self.config.janitor_interval):
            try:
                self.janitor_once()
            except Exception as exc:  # pragma: no cover - 常驻兜底
                print(f"[hub] janitor: {type(exc).__name__}: {exc}", flush=True)

    def _cloudrun_loop(self) -> None:
        # 节奏与 janitor 相同；tick 自己有单飞锁，两轮不会重叠。
        while not self._stop.wait(self.config.janitor_interval):
            self.cloudrun_once()

    def cloudrun_once(self) -> None:
        """跑一轮 Cloud Run 对账 + 拉起（测试可直接调用）。"""
        if self.cloudrun is None:
            return
        try:
            self.cloudrun.tick()
        except Exception as exc:  # pragma: no cover - 兜底
            print(f"[hub] cloudrun: {type(exc).__name__}: {exc}", flush=True)

    def ephemeral_hosts(self) -> list[str]:
        return [name for name, spec in self.config.hosts.items()
                if spec.provider == "cloudrun"]

    def janitor_once(self) -> None:
        # 不在这里调 gcloud：Cloud Run 的 tick 在自己的线程里（start_cloudrun）。
        for job in self.store.reap_stale_hosts(skip_hosts=self.ephemeral_hosts()):
            print(f"[hub] reaped {job['id']}: {job.get('error', '')}", flush=True)
        for job in self.store.reap_queue_holds():
            print(f"[hub] queue hold expired: {job['id']}: "
                  f"{job.get('error', '')}", flush=True)
        for job in self.store.expired_jobs(now()):
            files = self.store.purge_job(job["id"])
            self._unlink(files)
            artifact_dir = self.artifact_dir / job["id"]
            for path in artifact_dir.glob("*") if artifact_dir.exists() else []:
                self._unlink([str(path)])
            try:
                artifact_dir.rmdir()
            except OSError:
                pass
        for path in self.store.purge_orphan_bundles(now()):
            self._unlink([path])
        self._prune_keep_refs()
        self.store.prune_events(self.config.event_retention)
        self.store.prune_webhook_deliveries(
            now() - self.config.webhook_retention_seconds)
        self.store.prune_idempotency(self.config.idempotency_window_seconds)
        self.store.checkpoint()

    def _prune_keep_refs(self) -> None:
        cutoff = now() - self.keep_ref_seconds
        for mirror in self.mirror_dir.glob("*.git"):
            try:
                gitops.prune_keep_refs(mirror, cutoff)
            except Exception:
                pass

    @staticmethod
    def _unlink(paths: list[str]) -> None:
        for path in paths:
            try:
                Path(path).unlink()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        if self._webhooks is not None:
            self._webhooks.stop()

    # ------------------------------------------------------------ 项目/bundle
    def mirror_for(self, project: str) -> Path:
        if not valid_project_name(project):
            raise ValueError(f"invalid project name: {project!r}")
        path = self.mirror_dir / f"{project}.git"
        gitops.ensure_mirror(path)
        self.store.ensure_project(project, str(path))
        return path

    def ingest_bundle(self, project: str, bundle_path: Path) -> list[dict]:
        mirror = self.mirror_for(project)
        return gitops.ingest_bundle(mirror, bundle_path)

    def have_commits(self, project: str, commits: list[str]) -> list[str]:
        if not valid_project_name(project):
            raise ValueError(f"invalid project name: {project!r}")
        mirror = self.mirror_dir / f"{project}.git"
        if not (mirror / "HEAD").exists():
            return []
        return gitops.have_commits(mirror, commits)

    def bundle_path(self, bundle_id: str) -> Path | None:
        """只接受登记过的 bundle id，并确认解析后的路径在 bundles 目录内（B4）。"""
        row = self.store.bundle(bundle_id)
        if row is None:
            return None
        path = Path(row["path"]).resolve()
        root = self.bundles.resolve()
        if not path.is_relative_to(root):
            return None
        return path if path.exists() else None

    def thick_bundle(self, project: str, sha: str) -> Path | None:
        """从镜像现打一个自足 bundle（A4：临时文件 + os.replace + 每 sha 一把锁）。"""
        mirror = self.mirror_dir / f"{project}.git"
        if not sha or not (mirror / "HEAD").exists() or not gitops.has_commit(mirror, sha):
            return None
        target = self.bundles / f"full-{project}-{sha}.bundle"
        if target.exists():
            return target
        with self._thick_guard:
            lock = self._thick_locks.setdefault(sha, threading.Lock())
        with lock:
            if target.exists():
                return target
            ref = f"refs/calltable/keep/{sha}"
            gitops.git(["-C", str(mirror), "update-ref", ref, sha], check=False)
            tmp = self.bundles / f".{uuid.uuid4().hex}.tmp"
            try:
                gitops.git(["-C", str(mirror), "bundle", "create", str(tmp), ref])
                tmp.replace(target)
            except gitops.GitError:
                tmp.unlink(missing_ok=True)
                return None
            finally:
                with self._thick_guard:
                    self._thick_locks.pop(sha, None)
        return target

    # ------------------------------------------------------------ 入队
    def create_job(self, body: dict) -> dict:
        """校验并创建任务；失败抛 ValueError（HTTP 层转 400）。"""
        requested_project = str(body.get("project") or "").strip()
        project = requested_project
        host = str(body.get("host") or "").strip()
        cmd = body.get("cmd") or []
        if not valid_project_name(project):
            raise ValueError(f"invalid project name: {requested_project!r}")
        if not host:
            raise ValueError("host is required")
        if not isinstance(cmd, list) or not cmd:
            raise ValueError("cmd must be a non-empty array")
        groups = self.config.groups
        group_name = ""
        if host in groups:
            group_name = host
            targets = list(groups[host])
        else:
            targets = [host]
        if self.config.hosts and not any(member in self.config.hosts
                                        for member in targets):
            raise ValueError(f"unknown host or group: {host} (configured hosts: "
                             f"{', '.join(sorted(self.config.hosts))}; groups: "
                             f"{', '.join(sorted(groups)) or '-'})")
        if group_name:
            targets = self._group_targets(group_name, project, targets)
        host_spec = self.config.hosts.get(targets[0])
        locks = [str(x) for x in (body.get("locks") or [])]
        # profile 由 hub 按「项目 + 组内首选主机」解析；执行时再按实际主机
        # 重新解析，这样 project@<actual host> 的按主机覆盖才生效。
        profile = find_profile(self.profiles, project, targets[0])
        if profile is not None and profile.unsupported:
            raise ValueError(f"profile {profile.key} is unsupported: {profile.note}")
        if profile is not None and profile.project != project:
            raise ValueError("profile does not belong to this project")
        timeout_value = body.get("timeout")
        # 老客户端（没有 rules 逻辑）会把 profile 默认值当成「显式值」发上来，
        # 结果就是门禁被提交成 light。请求里没带版本、或版本低于引入 rules 的
        # 那个版本时，忽略它的 slot/locks/timeout，完全按 rules 重算。
        client_version = str(body.get("client_version") or "")
        legacy_client = (version_tuple(client_version)
                         < version_tuple(RULES_CLIENT_VERSION))
        if legacy_client:
            if body.get("slot") or locks or timeout_value is not None:
                self._warn(f"legacy client {client_version or '(unknown)'} "
                           "sent slot/locks/timeout; ignoring them and "
                           "applying the profile rules instead")
            locks = []
            timeout_value = None
        # 命令行显式写的值才覆盖；规则/profile 默认值在这里统一算出，
        # 客户端只负责把同样的规则结果打印出来。
        resolution = resolve_job(
            profile, [str(x) for x in cmd],
            slot=(None if legacy_client
                  else (str(body["slot"]) if body.get("slot") else None)),
            locks=locks,
            timeout=(None if legacy_client or timeout_value is None
                     else parse_duration(timeout_value)),
            no_lock=bool(body.get("no_lock")),
            default_slot=self.config.default_slot)
        slot = resolution.slot
        locks = resolution.locks
        schedulable = [member for member in targets
                       if member in self.config.hosts
                       and int((self.config.hosts[member].slots or {}).get(slot, 0)) > 0]
        if self.config.hosts and not schedulable:
            raise ValueError(f"slot {slot!r} is not available on any host of "
                             f"{host!r} ({', '.join(targets)})")
        if len(set(locks)) != len(locks):
            raise ValueError("duplicate locks")
        for lock in locks:
            if not re.fullmatch(r"[A-Za-z0-9._{}-]+(@[A-Za-z0-9._{}-]+)?", lock):
                raise ValueError(f"bad lock name: {lock!r}")
        if host_spec and host_spec.slots and slot not in host_spec.slots:
            raise ValueError(f"unknown slot {slot!r} on {host}; "
                             f"available: {', '.join(sorted(host_spec.slots))}")
        timeout = resolution.timeout
        bundle_id = str(body.get("bundle_id") or "")
        if bundle_id:
            row = self.store.bundle(bundle_id)
            if row is None:
                raise ValueError("unknown bundle_id")
            if row["project"] != project:
                raise ValueError("bundle belongs to another project")
            if self.bundle_path(bundle_id) is None:
                raise ValueError("bundle path rejected")
        webhooks = self._job_webhooks(body)
        hold_seconds = self._job_hold_seconds(body)
        artifact_patterns = self._job_artifact_patterns(body)
        idempotency = self._job_idempotency(
            body, project=project, host=host, cmd=cmd,
            sha=str(body.get("sha") or ""),
            tree_sha=str(body.get("tree_sha") or "").strip(),
            base_ref=str(body.get("base_ref") or ""),
            dirty=bool(body.get("dirty")), slot=slot, locks=locks,
            timeout=timeout, label=str(body.get("label") or ""),
            artifact_patterns=artifact_patterns)
        job = self.store.create_job(
            project=project, host=host, slot=slot, locks=locks, cmd=cmd,
            profile=(profile.name if profile else ""),
            submitter=str(body.get("submitter") or ""),
            agent=str(body.get("agent") or ""),
            label=str(body.get("label") or ""),
            sha=str(body.get("sha") or ""),
            base_ref=str(body.get("base_ref") or ""),
            base_ref_sha=str(body.get("base_ref_sha") or ""),
            base_sha=str(body.get("base_sha") or ""),
            dirty=bool(body.get("dirty")),
            timeout=timeout, bundle_id=bundle_id, group_name=group_name,
            targets=targets, webhooks=webhooks, hold_seconds=hold_seconds,
            idempotency=idempotency, artifact_patterns=artifact_patterns)
        return job

    @staticmethod
    def _job_artifact_patterns(body: dict) -> list[str] | None:
        """仓库声明的产物 glob：缺省（None）表示沿用 hub profile。"""
        raw = body.get("artifact_patterns")
        if raw is None:
            return None
        if not isinstance(raw, list) or len(raw) > MAX_ARTIFACT_PATTERNS:
            raise ValueError("artifact_patterns must be an array of at most "
                             f"{MAX_ARTIFACT_PATTERNS} strings")
        patterns: list[str] = []
        for index, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise ValueError("artifact_patterns must be non-empty strings")
            pattern = item.strip()
            # 和客户端 repoconf 同一套检查（相对路径、长度、通配段数）。
            problem = artifact_pattern_problem(pattern)
            if problem:
                raise ValueError(f"artifact_patterns[{index}] {problem}")
            patterns.append(pattern)
        return patterns

    def _group_targets(self, group: str, project: str,
                       members: list[str]) -> list[str]:
        """组任务的目标：provider 主机（Cloud Run）只有在项目为它单独写了
        ``<project>@<该主机>`` profile（且不是 unsupported）时才保留。

        没有镜像 / job 定义的项目溢出到 Cloud Run 只会拉起失败；通配 profile
        （``<project>@*``、``*@<host>``）不算数。直接写 ``--host <provider 主机>``
        的提交不经过这里，行为不变。

        一个组任务最多保留**一台** provider 主机（第一台符合条件的）：
        ``cloud_executions`` 按任务 id 记一条，第二台的拉起会覆盖第一台的
        花费和待删 git ref 记录。
        """
        kept, dropped = [], []
        provider_kept = False
        for member in members:
            spec = self.config.hosts.get(member)
            if spec is not None and spec.provider == "cloudrun":
                if provider_kept or not self._has_host_profile(project, member):
                    dropped.append(member)
                    continue
                provider_kept = True
            kept.append(member)
        if not kept:
            raise ValueError(
                f"no host of group {group!r} can run project {project!r}: "
                f"{', '.join(dropped)} only take group jobs of projects with a "
                f"{project}@<host> profile")
        return kept

    def _has_host_profile(self, project: str, host: str) -> bool:
        profile = self.profiles.get(f"{project}@{host}")
        return (profile is not None and not profile.unsupported
                and profile.project == project and profile.host == host)

    # ------------------------------------------------------------ 0.3 提交扩展
    def _job_webhooks(self, body: dict) -> list[str]:
        raw = body.get("webhooks") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise ValueError("webhooks must be an array of URLs")
        urls: list[str] = []
        for value in raw:
            url = validate_url(value)
            if url not in urls:
                urls.append(url)
        if len(urls) > MAX_JOB_WEBHOOKS:
            raise ValueError(f"at most {MAX_JOB_WEBHOOKS} webhooks per job")
        if urls and not self.config.webhook_secret:
            raise ValueError(
                "this hub has no webhook signing secret configured "
                "(webhook_secret); --notify is unavailable")
        return urls

    def _job_hold_seconds(self, body: dict) -> int:
        try:
            hold = int(body.get("hold_seconds") or 0)
        except (TypeError, ValueError):
            raise ValueError("hold_seconds must be an integer") from None
        if hold < 0:
            raise ValueError("hold_seconds must be >= 0")
        return min(hold, 7 * 86400)

    def _job_idempotency(self, body: dict, *, project: str, host: str,
                         cmd: list[str], sha: str, tree_sha: str,
                         base_ref: str, dirty: bool,
                         slot: str, locks: list[str], timeout: int,
                         label: str,
                         artifact_patterns: list[str] | None = None
                         ) -> dict | None:
        key = str(body.get("idempotency_key") or "").strip()
        if not key:
            return None
        if not IDEMPOTENCY_RE.fullmatch(key):
            raise ValueError(
                "idempotency key must be 1-200 characters of A-Z a-z 0-9 . _ : -")
        if tree_sha and not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
            raise ValueError("tree_sha must be a 40-character hex object id")
        # 代码身份也进入指纹：干净树 = commit sha；脏树 = 快照 tree hash。
        # 重试同样的内容会复用，内容一变就 409，避免把旧结果当成新代码的结果。
        if dirty and tree_sha:
            code_identity = "tree:" + tree_sha
        else:
            code_identity = "commit:" + sha
        extra = ({} if artifact_patterns is None
                 else {"artifact_patterns": list(artifact_patterns)})
        fingerprint = submission_fingerprint(
            project=project, host=host, cmd=list(cmd), base_ref=base_ref,
            dirty=bool(dirty), code_identity=code_identity, slot=slot,
            locks=list(locks), timeout=timeout, label=label, **extra)
        return {"key": key, "fingerprint": fingerprint,
                "window": self.config.idempotency_window_seconds}

    def register_bundle(self, project: str, path: Path, size: int) -> dict:
        """登记一次已落盘的上传，执行 ingest 并返回 bundle id（B4）。"""
        if not valid_project_name(project):
            raise ValueError(f"invalid project name: {project!r}")
        heads = self.ingest_bundle(project, path)
        bundle_id = path.stem
        self.store.add_bundle(bundle_id, project, str(path), size)
        return {"bundle_id": bundle_id, "heads": heads, "size": size}

    # ------------------------------------------------------------ 心跳对账
    def heartbeat(self, host: str, worker: str, running: list[str],
                  os_name: str = "", version: str = "",
                  system: dict | None = None) -> dict:
        if not host or not worker:
            raise ValueError("host and worker are required")
        spec = self.config.hosts.get(host)
        if spec is None:
            self._warn(f"host {host} is not configured; it will get 0 slots")
        reaped = self.store.heartbeat(
            host, worker, running, os_name, version, system=system,
            ephemeral=bool(spec is not None and spec.ephemeral))
        return {"reaped": reaped, "host": self.store.host(host)}

    def claim(self, host: str, worker: str) -> dict | None:
        spec = self.config.hosts.get(host)
        if spec is not None and spec.ephemeral:
            # provider 主机的任务由 hub 预留，执行器只能 attach 指定任务。
            self._warn(f"host {host} uses provider {spec.provider!r}; executors "
                       "must use `exec --once --job-id`, refusing plain claim")
            return None
        limits = spec.slots if spec else {}
        if not limits:
            self._warn(f"host {host} has no slots configured; refusing to dispatch "
                       "(limits are 0)")
            return None
        job = self.store.claim(host, worker)
        if job is None:
            return None
        return self._exec_payload(job)

    def attach(self, job_id: str, host: str, worker: str) -> dict | None:
        """一次性执行器认领 provider 为它预留的任务。"""
        spec = self.config.hosts.get(host)
        if spec is None or not spec.ephemeral:
            return None
        job = self.store.attach_job(job_id, host, worker)
        if job is None:
            return None
        return self._exec_payload(job)

    def _warn(self, message: str) -> None:
        if message in self._warned:
            return
        self._warned.add(message)
        print(f"[hub] WARNING: {message}", flush=True)

    # ------------------------------------------------------------ 队列可见性
    def with_queue_info(self, jobs: list[dict]) -> list[dict]:
        """给排队中的任务补上位置、预计等待与预计耗时（见 spec §3）。"""
        if not any(job.get("status") == QUEUED for job in jobs):
            return jobs
        info = self.store.queue_info()
        estimates = self.store.queue_estimates()
        for job in jobs:
            if job.get("status") != QUEUED:
                continue
            extra = info.get(job["id"]) or {"queued_seconds": 0, "ahead": 0}
            est = estimates.get(job["id"]) or {}
            wait = est.get("wait_seconds")
            job["ahead"] = int(extra.get("ahead", 0))
            job["queued_seconds"] = int(extra.get("queued_seconds", 0))
            job["queue"] = {
                "ahead": job["ahead"],
                "queued_seconds": job["queued_seconds"],
                "targets": list(job.get("targets") or []),
                "group": job.get("group_name") or "",
                "wait_seconds": wait,
                "runtime_seconds": est.get("runtime_seconds"),
                "starts_in_seconds": wait,
                "known": bool(est.get("known")),
                "runtime_samples": int(est.get("runtime_samples") or 0),
                "runtime_basis": est.get("runtime_basis") or "",
                "wait_unknown_blockers": int(est.get("wait_unknown_blockers") or 0),
            }
        return jobs

    def with_webhook_info(self, job: dict) -> dict:
        """详情接口：每个 webhook 的两条事件投递状态。"""
        job["webhook_deliveries"] = deliveries_for_display(
            self.store.webhook_deliveries(job["id"]))
        return job

    # ------------------------------------------------------------ 长轮询
    @staticmethod
    def _wait_satisfied(job: dict, until: str) -> bool:
        if job.get("status") in TERMINAL:
            return True
        if until == "started":
            return job.get("started") is not None
        return False

    def wait_for_job(self, job_id: str, until: str = "done",
                     timeout: float = 0.0) -> tuple[dict | None, str]:
        """阻塞到状态变化/条件满足/超时；返回 (job, done|timeout|missing)。"""
        deadline = now() + max(0.0, float(timeout))
        while True:
            # 先取 seq 再读状态：如果期间有变化，wait_change 会立即返回。
            seq = self.store.change_seq
            job = self.store.job(job_id)
            if job is None:
                return None, "missing"
            if self._wait_satisfied(job, until):
                return job, "done"
            remaining = deadline - now()
            if remaining <= 0:
                return job, "timeout"
            self.store.wait_change(seq, remaining)

    # ------------------------------------------------------------ 执行器载荷
    def _exec_payload(self, job: dict) -> dict:
        profile = self.hub_profile(job)
        bundle = self.store.job_bundle(job["id"])
        payload = dict(job)
        payload["bundle_url"] = (f"/api/jobs/{job['id']}/bundle" if bundle else "")
        payload["profile_data"] = profile_to_dict(profile) if profile else {}
        if job.get("artifact_patterns") is not None:
            # 仓库声明的产物 glob 取代 hub profile 的（命令行 > 声明 > profile）。
            payload["profile_data"]["artifacts"] = list(job["artifact_patterns"])
        # 按实际执行主机取档位（与 hub_profile 同一台）：主机级覆盖 hub 级。
        tier = self.config.host_resource_tier(job["host"], job["slot"])
        rule, _ = match_rule(profile, job.get("cmd") or [])
        profile_memory = None
        if profile is not None and profile.slot == job["slot"]:
            profile_memory = profile.memory_max
        payload["systemd"] = {
            "memory_max": ((rule.memory_max if rule else None)
                           or profile_memory
                           or tier.get("memory_max")),
            "cpu_weight": (str((profile.cpu_weight if profile else None)
                               or tier.get("cpu_weight") or "")),
            "slice": ((profile.slice if profile else None)
                      or tier.get("slice")),
        }
        payload["cpu_rate_percent"] = self.config.cpu_rate_percent
        payload["server_time"] = now()
        payload["hub_time"] = iso(now())
        return payload

    def hub_profile(self, job: dict) -> Profile | None:
        """按实际执行主机解析 profile，优先 project@<actual host>。"""
        profile = find_profile(self.profiles, job["project"], job["host"])
        if profile is None and job.get("profile"):
            profile = self.profiles.get(job["profile"])
        return profile


def submission_fingerprint(**fields) -> str:
    """幂等指纹：只覆盖语义参数（bundle_id/传输身份不参与，见 spec §8）。"""
    payload = json_dumps({key: fields[key] for key in sorted(fields)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def profile_to_dict(profile: Profile | None) -> dict:
    if profile is None:
        return {}
    return {
        "name": profile.name,
        "project": profile.project,
        "host": profile.host,
        "slot": profile.slot,
        "locks": profile.locks,
        "env_files": profile.env_files,
        "env": profile.env,
        "setup": profile.setup,
        "artifacts": profile.artifacts,
        "timeout": profile.timeout,
        "memory_max": profile.memory_max,
        "cpu_weight": profile.cpu_weight,
        "slice": profile.slice,
    }


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"calltable/{__version__}"

    @property
    def hub(self) -> Hub:
        return self.server.hub  # type: ignore[attr-defined]

    # ---- 基础 -------------------------------------------------------
    def log_message(self, fmt, *args):
        """日志里把 query 脱敏：token 可能出现在产物下载链接里（A9）。"""
        message = fmt % args
        message = re.sub(r"(token=)[^&\s\"]+", r"\1***", message)
        if args and str(args[1]).startswith(("4", "5")):
            print(f"[hub] {self.address_string()} {message}", flush=True)

    def _send(self, status: int, body: bytes | str = b"",
              ctype: str = "application/json", headers: dict | None = None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload, headers: dict | None = None):
        self._send(status, json.dumps(payload, ensure_ascii=False), headers=headers)

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def _stream_file(self, path: Path, ctype: str = "application/octet-stream",
                     headers: dict | None = None):
        """流式发送文件；支持 ``Range: bytes=<start>-`` 断点续传（206）。"""
        try:
            size = path.stat().st_size
            handle = path.open("rb")
        except OSError:
            return self._error(404, "file not found")
        start = 0
        range_header = self.headers.get("Range", "") if self.headers else ""
        match = re.fullmatch(r"bytes=(\d+)-\d*", (range_header or "").strip())
        if match:
            start = int(match.group(1))
            if start >= size:
                handle.close()
                return self._error(416, "range not satisfiable")
        with handle:
            handle.seek(start)
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size - start))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            while True:
                block = handle.read(IO_BLOCK)
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    return

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and \
            header[7:].strip() == self.hub.config.token_value

    def _authorized_artifact_query(self) -> bool:
        """唯一允许 query token 的端点：GET 产物下载（A9）。"""
        token = (self.query.get("token") or [""])[0]
        return token == self.hub.config.token_value

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            raise ValueError(f"body too large: {length}")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _save_body(self, dest: Path, limit: int) -> int:
        """流式落盘（I7）：不把整个上传读进内存。返回字节数。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError(f"body too large: {length} > {limit}")
        remaining = length
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            while remaining > 0:
                block = self.rfile.read(min(IO_BLOCK, remaining))
                if not block:
                    break
                handle.write(block)
                remaining -= len(block)
        if remaining:
            raise ValueError("incomplete request body")
        return length

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        self.query = urllib.parse.parse_qs(parsed.query)
        return parsed.path

    # ---- GET --------------------------------------------------------
    def do_GET(self):
        path = self._route()
        try:
            if path == "/api/health":
                return self._json(200, {
                    "ok": True, "version": __version__,
                    "rules_client_version": RULES_CLIENT_VERSION,
                    "capabilities": list(CAPABILITIES),
                    "webhooks": {
                        "enabled": bool(self.hub.config.webhook_secret),
                        "signature_version": "v1" if self.hub.config.webhook_secret else "",
                    },
                    "time": iso(now())})
            if path == "/":
                token = (self.query.get("token") or [""])[0]
                injected = token if token == self.hub.config.token_value else ""
                return self._send(200, render_page(injected,
                                                   self.hub.config.beszel_url),
                                  ctype="text/html; charset=utf-8")

            match = re.fullmatch(r"/api/jobs/([^/]+)/artifacts/(.+)", path)
            if match and not self._authorized():
                if not self._authorized_artifact_query():
                    return self._error(401, "unauthorized")
                return self._serve_artifact(match.group(1),
                                            urllib.parse.unquote(match.group(2)))
            if not self._authorized():
                return self._error(401, "unauthorized")

            if path == "/api/events":
                return self._stream_events()
            if path == "/api/state":
                return self._json(200, self._state())
            if path == "/api/hosts":
                return self._json(200, {"hosts": self.hub.store.hosts(),
                                        "groups": self.hub.config.groups})
            if path == "/api/cloudrun":
                summary = (self.hub.cloudrun.summary()
                           if self.hub.cloudrun is not None else {})
                return self._json(200, {"hosts": summary})
            if path == "/api/groups":
                return self._json(200, {"groups": self.hub.config.groups,
                                        "hosts": self.hub.store.hosts()})
            if path == "/api/jobs":
                status = (self.query.get("status") or [None])[0]
                limit = int((self.query.get("limit") or ["100"])[0])
                jobs = self.hub.store.jobs(status, limit)
                return self._json(200, {"jobs": self.hub.with_queue_info(jobs)})

            match = re.fullmatch(r"/api/jobs/([^/]+)", path)
            if match:
                return self._job_detail(match.group(1))

            match = re.fullmatch(r"/api/jobs/([^/]+)/logs", path)
            if match:
                offset = int((self.query.get("offset") or ["0"])[0])
                data, next_offset = self.hub.store.read_logs(match.group(1), offset)
                return self._send(200, data, ctype="application/octet-stream",
                                  headers={"X-Calltable-Next": str(next_offset)})

            match = re.fullmatch(r"/api/jobs/([^/]+)/samples", path)
            if match:
                return self._json(200, {"samples": self.hub.store.samples(match.group(1))})

            match = re.fullmatch(r"/api/jobs/([^/]+)/artifacts", path)
            if match:
                return self._json(200, {"artifacts": self.hub.store.artifacts(match.group(1))})

            match = re.fullmatch(r"/api/jobs/([^/]+)/artifacts/(.+)", path)
            if match:
                return self._serve_artifact(match.group(1),
                                            urllib.parse.unquote(match.group(2)))

            match = re.fullmatch(r"/api/jobs/([^/]+)/bundle", path)
            if match:
                job = self.hub.store.job(match.group(1))
                if not job:
                    return self._error(404, "no such job")
                if (self.query.get("full") or [""])[0] == "1":
                    wanted = (self.query.get("sha") or [job["sha"]])[0]
                    bundle = self.hub.thick_bundle(job["project"], wanted)
                    if bundle is None:
                        return self._error(409, "hub mirror has no such commit")
                    return self._stream_file(bundle)
                bundle = self.hub.store.job_bundle(job["id"])
                if not bundle:
                    return self._send(204, b"", ctype="application/octet-stream")
                path_obj = Path(bundle["path"]).resolve()
                if not path_obj.is_relative_to(self.hub.bundles.resolve()):
                    return self._error(410, "bundle path rejected")
                if not path_obj.exists():
                    return self._error(410, "bundle no longer available")
                return self._stream_file(path_obj)

            match = re.fullmatch(r"/api/projects/([^/]+)/commits", path)
            if match:
                project = match.group(1)
                if not valid_project_name(project):
                    return self._error(400, "invalid project name")
                mirror = self.hub.mirror_dir / f"{project}.git"
                commits = []
                if (mirror / "HEAD").exists():
                    limit = int((self.query.get("limit") or ["500"])[0])
                    out = gitops.git(["-C", str(mirror), "rev-list",
                                      f"--max-count={limit}", "--all"], check=False)
                    commits = [line for line in out.stdout.splitlines() if line]
                return self._json(200, {"commits": commits})

            return self._error(404, "not found")
        except Exception as exc:  # pragma: no cover - 兜底
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def _serve_artifact(self, job_id: str, name: str):
        art = self.hub.store.artifact(job_id, name)
        if not art:
            return self._error(404, "no such artifact")
        # 两边都要 resolve()：temp/data 目录本身可能就是符号链接（macOS 的
        # /var -> /private/var、Windows 的 8.3 短名），只 resolve 一边会让合法
        # 路径被当成越界，返回 410。
        path = Path(art["stored"]).resolve()
        if not path.is_relative_to(self.hub.artifact_dir.resolve()):
            return self._error(410, "artifact path rejected")
        if not path.exists():
            return self._error(404, "artifact removed")
        return self._stream_file(path, headers={"X-Calltable-SHA256": art["sha256"]})

    def _job_detail(self, job_id: str):
        """GET /api/jobs/<id>，支持 ``wait`` / ``until`` 长轮询。"""
        until = (self.query.get("until") or ["done"])[0] or "done"
        if until not in ("started", "done"):
            return self._error(400, "until must be started or done")
        wait_raw = (self.query.get("wait") or [""])[0]
        headers: dict[str, str] = {}
        if str(wait_raw).strip():
            try:
                seconds = float(wait_raw)
            except (TypeError, ValueError):
                return self._error(400, "wait must be a number of seconds")
            seconds = max(0.0, min(seconds, self.hub.config.max_wait_seconds))
            if self.hub.store.job(job_id) is None:
                return self._error(404, "no such job")
            if not self.hub.wait_slots.acquire(blocking=False):
                return self._send(
                    503, json.dumps({"error": "too many concurrent waits"}),
                    headers={"Retry-After": "1"})
            try:
                job, result = self.hub.wait_for_job(job_id, until, seconds)
            finally:
                self.hub.wait_slots.release()
            if job is None:
                return self._error(404, "no such job")
            headers = {"X-Calltable-Wait-Result": result,
                       "X-Calltable-Wait-Until": until}
        else:
            job = self.hub.store.job(job_id)
            if not job:
                return self._error(404, "no such job")
        job["artifacts"] = self.hub.store.artifacts(job["id"])
        job = self.hub.with_webhook_info(job)
        return self._json(200, self.hub.with_queue_info([job])[0],
                          headers=headers or None)

    # ---- SSE --------------------------------------------------------
    def _sse_write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def _client_gone(self) -> bool:
        """非阻塞探测客户端是否已经断开，及时释放流名额（不等下一次写）。"""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def _stream_events(self):
        """GET /api/events：带事件 id 与心跳注释的 SSE 流。"""
        job_filter = (self.query.get("job") or [""])[0] or None
        if job_filter and self.hub.store.job(job_filter) is None:
            return self._error(404, "no such job")
        raw = (self.headers.get("Last-Event-ID")
               if self.headers else None) or (self.query.get("after") or [""])[0]
        try:
            after = int(str(raw).strip()) if str(raw or "").strip() else 0
        except ValueError:
            after = 0
        if after <= 0:
            # 不传游标 = 只看从现在开始的新事件，避免重放整个日志。
            after = self.hub.store.last_event_seq()
        if not self.hub.event_slots.acquire(blocking=False):
            return self._send(503, json.dumps(
                {"error": "too many event streams"}),
                headers={"Retry-After": "1"})
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse_write("retry: 1000\n\n")
            heartbeat = self.hub.config.event_heartbeat_seconds
            last_write = time.time()
            while not self.hub._stop.is_set():
                if self._client_gone():
                    break
                events = self.hub.store.events_after(after, job_filter, limit=200)
                if events:
                    for event in events:
                        frame = (f"id: {event['seq']}\n"
                                 f"event: {event['type']}\n"
                                 f"data: {json_dumps(event['data'])}\n\n")
                        self._sse_write(frame)
                        after = int(event["seq"])
                    last_write = time.time()
                    continue
                if time.time() - last_write >= heartbeat:
                    self._sse_write(": keepalive\n\n")
                    last_write = time.time()
                seq = self.hub.store.change_seq
                self.hub.store.wait_change(seq, min(heartbeat, 1.0))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:  # pragma: no cover - 流已开始，不能再写 JSON 错
            print(f"[hub] event stream: {type(exc).__name__}: {exc}", flush=True)
        finally:
            self.hub.event_slots.release()

    def do_POST(self):
        path = self._route()
        try:
            if not self._authorized():
                return self._error(401, "unauthorized")

            if path == "/api/hosts/heartbeat":
                body = self._body_json()
                result = self.hub.heartbeat(
                    str(body.get("host") or "").strip(),
                    str(body.get("worker") or "").strip(),
                    [str(x) for x in (body.get("running") or [])],
                    body.get("os", ""), body.get("version", ""),
                    body.get("system") or None)
                return self._json(200, {"ok": True, "reaped": result["reaped"],
                                        "host": result["host"]})

            if path == "/api/exec/claim":
                body = self._body_json()
                host = str(body.get("host") or "").strip()
                worker = str(body.get("worker") or "").strip()
                if not host or not worker:
                    return self._error(400, "host and worker are required")
                self.hub.store.touch_host(host)
                job = self.hub.claim(host, worker)
                if job is None:
                    return self._send(204, b"", ctype="application/json")
                return self._json(200, job)

            match = re.fullmatch(r"/api/exec/jobs/([^/]+)/attach", path)
            if match:
                body = self._body_json()
                host = str(body.get("host") or "").strip()
                worker = str(body.get("worker") or "").strip()
                if not host or not worker:
                    return self._error(400, "host and worker are required")
                job = self.hub.attach(match.group(1), host, worker)
                if job is None:
                    return self._error(409, "job is not reserved for this "
                                            "executor (not running on this "
                                            "provider host, or already attached)")
                return self._json(200, job)

            match = re.fullmatch(r"/api/hosts/([^/]+)/drain", path)
            if match:
                body = self._body_json()
                drain = bool(body.get("drain", True))
                host = self.hub.store.set_draining(match.group(1), drain)
                if host is None:
                    return self._error(404, "no such host")
                return self._json(200, host)

            match = re.fullmatch(r"/api/exec/jobs/([^/]+)/finish", path)
            if match:
                body = self._body_json()
                status = body.get("status") or "error"
                if status not in TERMINAL:
                    return self._error(400, f"bad terminal status: {status}")
                job = self.hub.store.job(match.group(1))
                if not job:
                    return self._error(404, "no such job")
                # 已请求取消的任务一律记 cancelled（见 Store._finish_locked），
                # 保留期按最终状态算：取消可能在上面读快照之后才到，所以把
                # 失败保留期一并交给 store，在写终态的同一把锁里选。
                retention = (self.hub.config.retention_success
                             if status == "succeeded"
                             else self.hub.config.retention_failure)
                finished = self.hub.store.finish(
                    job["id"], status=status, exit_code=body.get("exit_code"),
                    error=str(body.get("error") or ""),
                    cleanup_at=now() + retention,
                    failure_cleanup_at=now() + self.hub.config.retention_failure)
                return self._json(200, finished)

            match = re.fullmatch(r"/api/jobs/([^/]+)/logs", path)
            if match:
                job_id = match.group(1)
                if not self.hub.store.job(job_id):
                    return self._error(404, "no such job")
                return self._append_log_stream(job_id)

            match = re.fullmatch(r"/api/jobs/([^/]+)/samples", path)
            if match:
                job_id = match.group(1)
                if not self.hub.store.job(job_id):
                    return self._error(404, "no such job")
                body = self._body_json()
                for sample in body.get("samples") or []:
                    self.hub.store.add_sample(
                        job_id, int(sample.get("cpu_usec") or 0),
                        int(sample.get("mem_current") or 0),
                        int(sample.get("mem_peak") or 0))
                return self._json(200, {"ok": True})

            match = re.fullmatch(r"/api/jobs/([^/]+)/artifacts", path)
            if match:
                job_id = match.group(1)
                if not self.hub.store.job(job_id):
                    return self._error(404, "no such job")
                name = urllib.parse.unquote(self.headers.get("X-Calltable-Path", ""))
                if not name:
                    return self._error(400, "X-Calltable-Path is required")
                return self._receive_artifact(job_id, name)

            match = re.fullmatch(r"/api/jobs/([^/]+)/cancel", path)
            if match:
                job = self.hub.store.request_cancel(match.group(1))
                if not job:
                    return self._error(404, "no such job")
                return self._json(200, job)

            match = re.fullmatch(r"/api/jobs/([^/]+)/heartbeat", path)
            if match:
                job = self.hub.store.touch_hold(match.group(1))
                if not job:
                    return self._error(404, "no such job")
                hold = int(job.get("hold_seconds") or 0)
                last = float(job.get("last_hold") or job.get("created") or now())
                expires = max(0, int(hold - (now() - last))) if hold else 0
                ok = job["status"] == QUEUED and hold > 0
                return self._json(200, {
                    "ok": ok, "job_id": job["id"],
                    "status": job["status"], "hold_seconds": hold,
                    "expires_in": expires})

            match = re.fullmatch(r"/api/projects/([^/]+)/have", path)
            if match:
                project = match.group(1)
                if not valid_project_name(project):
                    return self._error(400, "invalid project name")
                body = self._body_json()
                commits = [str(x) for x in (body.get("commits") or [])]
                return self._json(200, {
                    "have": self.hub.have_commits(project, commits)})

            match = re.fullmatch(r"/api/projects/([^/]+)/bundle/part", path)
            if match:
                return self._receive_bundle_part(match.group(1))

            match = re.fullmatch(r"/api/projects/([^/]+)/bundle/finalize", path)
            if match:
                return self._finalize_bundle(match.group(1))

            match = re.fullmatch(r"/api/projects/([^/]+)/bundle", path)
            if match:
                return self._receive_bundle(match.group(1))

            if path == "/api/jobs":
                return self._create_job()

            return self._error(404, "not found")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - 兜底
            return self._error(500, f"{type(exc).__name__}: {exc}")

    # ---- 上传 -------------------------------------------------------
    def _acquire_upload(self) -> bool:
        if not self.hub.upload_slots.acquire(timeout=30):
            self._error(503, "too many concurrent uploads")
            return False
        return True

    def _receive_bundle_part(self, project: str):
        """分块上传：按 offset 追加，断线后从服务端已有字节数续传。"""
        if not valid_project_name(project):
            return self._error(400, "invalid project name")
        upload_id = (self.query.get("upload_id") or [""])[0]
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            return self._error(400, "upload_id is required")
        try:
            offset = int((self.query.get("offset") or ["0"])[0])
        except ValueError:
            return self._error(400, "bad offset")
        if not self._acquire_upload():
            return
        target = self.hub.upload_dir / f"{upload_id}.part"
        try:
            current = target.stat().st_size if target.exists() else 0
            if offset != current:
                return self._json(409, {"upload_id": upload_id,
                                        "received": current})
            length = int(self.headers.get("Content-Length") or 0)
            if current + length > MAX_BUNDLE:
                return self._error(413, "bundle too large")
            remaining = length
            with target.open("ab") as handle:
                while remaining > 0:
                    block = self.rfile.read(min(IO_BLOCK, remaining))
                    if not block:
                        return self._error(400, "incomplete request body")
                    handle.write(block)
                    remaining -= len(block)
            return self._json(200, {"upload_id": upload_id,
                                    "received": target.stat().st_size})
        finally:
            self.hub.upload_slots.release()

    def _finalize_bundle(self, project: str):
        if not valid_project_name(project):
            return self._error(400, "invalid project name")
        body = self._body_json()
        upload_id = str(body.get("upload_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            return self._error(400, "upload_id is required")
        part = self.hub.upload_dir / f"{upload_id}.part"
        if not part.exists():
            return self._error(404, "upload not found")
        target = self.hub.bundles / f"{upload_id}.bundle"
        part.replace(target)
        try:
            result = self.hub.register_bundle(project, target,
                                              target.stat().st_size)
        except Exception as exc:
            target.unlink(missing_ok=True)
            return self._error(400, f"bundle rejected: {exc}")
        return self._json(200, result)

    def _receive_bundle(self, project: str):
        if not valid_project_name(project):
            return self._error(400, "invalid project name")
        if not self._acquire_upload():
            return
        try:
            bundle_id = uuid.uuid4().hex
            target = self.hub.bundles / f"{bundle_id}.bundle"
            size = self._save_body(target, MAX_BUNDLE)
            try:
                return self._json(200, self.hub.register_bundle(project, target, size))
            except Exception as exc:
                target.unlink(missing_ok=True)
                return self._error(400, f"bundle rejected: {exc}")
        finally:
            self.hub.upload_slots.release()

    def _receive_artifact(self, job_id: str, name: str):
        if not self._acquire_upload():
            return
        try:
            import hashlib
            target = self.hub.artifact_dir / job_id / f"{uuid.uuid4().hex}.bin"
            size = self._save_body(target, MAX_ARTIFACT)
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(IO_BLOCK), b""):
                    digest.update(block)
            self.hub.store.add_artifact(job_id, name, size, digest.hexdigest(),
                                        str(target))
            return self._json(200, {"name": name, "sha256": digest.hexdigest(),
                                    "size": size})
        finally:
            self.hub.upload_slots.release()

    def _append_log_stream(self, job_id: str):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_ARTIFACT:
            return self._error(413, "log chunk too large")
        remaining = length
        first = None
        truncated = False
        while remaining > 0:
            block = self.rfile.read(min(IO_BLOCK, remaining))
            if not block:
                return self._error(400, "incomplete request body")
            remaining -= len(block)
            start, cut = self.hub.store.append_log(job_id, block)
            if first is None:
                first = start
            truncated = truncated or cut
        return self._json(200, {"start": first or 0, "bytes": length,
                                "truncated": truncated})

    # ---- 业务 -------------------------------------------------------
    def _create_job(self):
        try:
            body = self._body_json()
            key = ""
            if self.headers:
                key = (self.headers.get("Idempotency-Key") or "").strip()
            if key and not body.get("idempotency_key"):
                body["idempotency_key"] = key
            job = self.hub.create_job(body)
            replay = bool(job.get("idempotent_replay"))
            payload = self.hub.with_queue_info([job])[0]
            headers = {"X-Calltable-Idempotent-Replay": "true"} if replay else None
            return self._json(200 if replay else 201, payload, headers=headers)
        except IdempotencyConflict as exc:
            return self._error(409, str(exc))
        except ValueError as exc:
            return self._error(400, str(exc))

    # ---- 看板聚合 ---------------------------------------------------
    def _state(self) -> dict:
        hosts = self.hub.store.hosts()
        running = [job for host in hosts for job in host["running"]]
        queued = self.hub.with_queue_info(
            self.hub.store.jobs(status=QUEUED, limit=200))
        for host in hosts:
            host["queue"] = sum(
                1 for job in queued
                if host["name"] == job["host"]
                or host["name"] in (job.get("targets") or []))
        queues: dict[str, int] = {}
        for job in queued:
            key = job.get("group_name") or job["host"]
            queues[key] = queues.get(key, 0) + 1
        return {
            "server_time": now(),
            "version": __version__,
            "beszel_url": self.hub.config.beszel_url,
            "hosts": hosts,
            "groups": self.hub.config.groups,
            "group_queue": queues,
            "running": running,
            "queued": queued,
            "jobs": self.hub.with_queue_info(self.hub.store.jobs(limit=100)),
            "stats": self.hub.store.stats(),
        }


class HubServer(ThreadingHTTPServer):
    """跳过 ``HTTPServer.server_bind`` 里的 ``socket.getfqdn()``。

    那句反查在 macOS 上走 mDNSResponder，没有解析服务时会卡几十秒，hub 在
    这段时间里根本还没 listen（监听 backlog 未建立），客户端只能看到连接被
    拒。``server_name`` 只给 CGI 环境变量用，这里不需要。
    """

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def serve(config: Config) -> None:
    hub = Hub(config)
    hub.start_janitor()
    hub.start_upstreams()
    server = HubServer((config.hub_bind, config.hub_port), Handler)
    server.hub = hub  # type: ignore[attr-defined]
    print(f"calltable hub listening on {config.hub_bind}:{config.hub_port} "
          f"(version {__version__}, data {config.data_dir})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.close()
        server.server_close()
