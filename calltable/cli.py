"""``tb`` 命令行：提交任务、看状态、跟日志、取消、看主机。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, gitops
from .client import HubClient, HubError
from .config import (DEFAULT_HUB_URL, DEFAULT_OWNER_TAILNET, Config,
                     ConfigError, load_config)
from .profiles import find_profile, load_profiles, resolve_job, resolve_host
from .seed import SeedError, mirror_root_for, seed as seed_mirror
from .util import human_bytes, human_duration, parse_duration

#: 超过这个大小的 bundle 会在慢链路上传很久，先提示去 seed。
SEED_WARN_BYTES = 20 * 1024 * 1024
#: 找共同祖先时回看的提交数。seed 进来的旧对象可能在几千个提交之前，
#: 需要覆盖整段历史（含分叉），否则祖先链判定会因截断而全部判为不安全。
CANDIDATE_DEPTH = 20000


def bundle_warning(size: int) -> str | None:
    if size <= SEED_WARN_BYTES:
        return None
    return (f"bundle is {human_bytes(size)} (> {human_bytes(SEED_WARN_BYTES)}); "
            "large uploads can take a long time on a slow link. Ask an admin to "
            "run `ct seed --project <p> --from <checkout>` on the hub and the "
            "executors first, or pass --yes to upload anyway.")


def _progress_printer():
    last = [0.0]

    def report(sent: int, total: int, elapsed: float) -> None:
        now = time.time()
        finishing = sent >= total
        if not finishing and now - last[0] < 1.0:
            return
        last[0] = now
        rate = sent / max(elapsed, 0.001)
        line = (f"upload {human_bytes(sent)}/{human_bytes(total)} "
                f"{human_bytes(rate)}/s")
        if sys.stderr.isatty():
            print("\r" + line + ("\n" if finishing else ""), end="",
                  file=sys.stderr, flush=True)
        elif finishing or sent == 0:
            print(line, file=sys.stderr, flush=True)

    return report


# ------------------------------------------------------------------ 输出

def _fmt_time(ts) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_mem(n) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}TB"


def _job_line(job: dict) -> str:
    dur = (job.get("finished") or time.time()) - job["started"] if job.get("started") else None
    exit_code = job.get("exit_code")
    tail = f" ({exit_code})" if exit_code is not None else ""
    queue = ""
    if job.get("status") == "queued":
        queue = (f" [queued {human_duration(job.get('queued_seconds', 0))}, "
                 f"{job.get('ahead', 0)} ahead]")
    return (f"{job['id']:<28} {job['project']:<10} {job['host']:<10} "
            f"{job['status']:<10}{tail:<6} {human_duration(dur):>8} "
            f"cpu={job.get('cpu_total_sec', 0):.1f}s/"
            f"{(job.get('cpu_peak_cores', 0)):.2f}c "
            f"mem={_fmt_mem(job.get('mem_peak'))} "
            f"{('[' + job['agent'] + '] ') if job.get('agent') else ''}"
            f"{job.get('label', '')}{queue}")


# ------------------------------------------------------------------ run

def make_submitter(repo: Path, agent: str = "") -> str:
    """默认提交人：<主机名>/<用户>/<仓库目录名>@<分支>。

    agent 单独存在 jobs.agent 里，不混进这个标识，避免 grep 时歧义。
    """
    host = socket.gethostname()
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - 极端环境
        user = os.environ.get("USER") or "unknown"
    branch = gitops.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                        check=False).stdout.strip()
    if not branch or branch == "HEAD":
        sha = gitops.rev_parse(repo, "HEAD") or "nogit"
        branch = sha[:8]
    return f"{host}/{user}/{Path(repo).name}@{branch}"


def prepare_submission(repo: Path, base_ref: str | None = "origin/main",
                       message: str = "calltable snapshot") -> dict:
    """决定这次提交要检出的提交、基线提交与基线 ref 的 sha。

    * 干净树：不造快照提交，``sha`` 就是 HEAD（证据可以指向真实提交）；
    * 有改动：``sha`` 是快照提交，``base_sha`` 是快照的父提交（原 HEAD）。

    ``base_ref_sha`` 是 ``base_ref`` 当前指向的提交，执行器会把它写成任务检出
    里的 ``origin/main``。
    """
    repo = Path(repo)
    dirty = gitops.is_dirty(repo)
    head = gitops.rev_parse(repo, "HEAD")
    if head is None:
        sha, _ = gitops.snapshot_commit(repo, message)
        dirty = True
        base_sha = None
    elif dirty:
        sha, _ = gitops.snapshot_commit(repo, message)
        base_sha = head
    else:
        sha, base_sha = head, head
    base_ref_sha = gitops.rev_parse(repo, base_ref) if base_ref else None
    return {
        "sha": sha,
        "dirty": dirty,
        "base_sha": base_sha or "",
        "base_ref": base_ref or "",
        "base_ref_sha": base_ref_sha or "",
    }


def plan_bundle(repo: Path, sha: str, base_ref_sha: str,
                hub_commits: set[str]) -> tuple[list[tuple[str, str]], list[str]] | None:
    """算出要上传哪些 tip、排除哪些已有提交；返回 None 表示 hub 已经全有。

    排除点用 ``gitops.safe_ancestor`` 选「自身和全部祖先都在 hub」的提交，
    避免 seed 镜像的洞导致 fetch 因缺少前置提交而失败。
    """
    include_base = bool(base_ref_sha) and base_ref_sha not in hub_commits
    if sha in hub_commits and not include_base:
        return None
    tips = [(sha, "snapshots")]
    if include_base:
        tips.append((base_ref_sha, "bases"))
    tip_shas = {tip for tip, _ in tips}
    excludes: list[str] = []
    for tip in tip_shas:
        if tip in hub_commits:
            continue
        common = gitops.newest_common(repo, tip, hub_commits,
                                      depth=CANDIDATE_DEPTH)
        if common and common not in tip_shas and common not in excludes:
            excludes.append(common)
    return tips, excludes


def cmd_run(config: Config, args) -> int:
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        repo = gitops.repo_root(args.repo or ".")
    except gitops.GitError:
        if not dry_run:
            raise
        # --dry-run 只做规则判定，不需要 git 仓库。
        repo = Path(args.repo or ".").resolve()
    profiles = load_profiles(config.profiles_dir)
    try:
        host = resolve_host(args.project, args.host, args.platform, profiles)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    agent = (args.agent or os.environ.get("CALLTABLE_AGENT", "") or "").strip()

    client = HubClient(args.hub or config.hub_url, args.token or config.token_value)
    # 组名展开成成员列表，用首个（最高优先级）成员的 profile 作为默认值。
    try:
        groups = client.get_json("/api/groups").get("groups", {})
    except HubError:
        groups = {}
    targets = groups.get(host, [host])
    profile = (find_profile(profiles, args.project, targets[0])
               or find_profile(profiles, args.project, host))
    if profile is not None and profile.unsupported:
        print(f"error: profile {profile.key} is unsupported: {profile.note}",
              file=sys.stderr)
        return 2
    # 最终生效的 slot / locks / timeout 由规则决定；命令行显式值优先，
    # hub 会用同一套规则再算一次（以 hub 的 profile 为准）。
    resolution = resolve_job(
        profile, args.command,
        slot=args.slot,
        locks=list(args.lock or []),
        timeout=(parse_duration(args.timeout) if args.timeout else None),
        no_lock=bool(getattr(args, "no_lock", False)),
        default_slot=config.default_slot,
    )
    locks = resolution.locks
    timeout = resolution.timeout
    if dry_run:
        print("dry-run (nothing submitted)")
        print(f"  project={args.project} host={host} "
              f"targets={','.join(targets)}")
        print(f"  profile={profile.name if profile else '-'}")
        print(f"  {resolution.describe()}")
        print(f"  command={' '.join(args.command)}")
        return 0
    submitter = args.submitter or make_submitter(repo, agent)
    base_ref = "" if args.no_base_ref else (args.base_ref or "")
    submission = prepare_submission(repo, base_ref,
                                    f"calltable snapshot ({submitter})")
    sha = submission["sha"]
    dirty = submission["dirty"]
    if base_ref and not submission["base_ref_sha"]:
        print(f"warning: base ref {base_ref!r} not found locally; "
              f"submitting without it", file=sys.stderr)
        base_ref = ""
        submission["base_ref"] = ""

    locks = list(args.lock or [])
    base = submission["base_ref_sha"] or None
    hub_commits = set(client.get_json(
        f"/api/projects/{args.project}/commits",
        params={"limit": 20000}).get("commits", []))
    plan = plan_bundle(repo, sha, submission["base_ref_sha"], hub_commits)
    bundle_path = None
    tmpdir = tempfile.mkdtemp(prefix="calltable-bundle-")
    try:
        if plan is not None:
            bundle_path = Path(tmpdir) / "upload.bundle"
            tips, excludes = plan
            gitops.create_bundle_multi(repo, tips, excludes, bundle_path)
        server_bundle = ""
        if bundle_path is not None:
            size = bundle_path.stat().st_size
            print(f"  bundle={human_bytes(size)}", flush=True)
            warning = bundle_warning(size)
            if warning and not args.yes:
                print(f"error: {warning}", file=sys.stderr)
                return 2
            started = time.time()
            result = client.upload_bundle(args.project, bundle_path,
                                          on_progress=_progress_printer())
            elapsed = time.time() - started
            if elapsed > 0:
                print(f"  uploaded {human_bytes(size)} in "
                      f"{human_duration(elapsed)} "
                      f"({human_bytes(size / elapsed)}/s)", flush=True)
            server_bundle = result["bundle_id"]
        else:
            print("  bundle=none (hub already has these commits)", flush=True)
        job = client.post_json("/api/jobs", {
            "project": args.project,
            "host": host,
            # 只送显式值；规则与 profile 默认值交给 hub 统一算。
            "slot": args.slot or None,
            "locks": locks,
            "no_lock": bool(getattr(args, "no_lock", False)),
            "client_version": __version__,
            "cmd": args.command,
            "profile": profile.name if profile else "",
            "submitter": submitter,
            "agent": agent,
            "label": args.label or "",
            "sha": sha,
            "base_ref": base_ref,
            "base_ref_sha": submission["base_ref_sha"],
            "base_sha": submission["base_sha"],
            "dirty": dirty,
            "timeout": (parse_duration(args.timeout)
                        if args.timeout else None),
            "bundle_id": server_bundle,
        })
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"queued {job['id']}")
    print(f"  project={job['project']} host={job['host']} slot={job['slot']} "
          f"locks={','.join(job.get('locks_display') or job['locks']) or '-'} "
          f"timeout={human_duration(job.get('timeout'))}")
    print(f"  rule={resolution.rule.match if resolution.rule else '(profile default)'}")
    if (job.get("slot") != resolution.slot
            or (job.get("locks") or []) != resolution.locks
            or int(job.get("timeout") or 0) != resolution.timeout):
        print(f"  note: hub resolved slot={job['slot']} "
              f"locks={','.join(job.get('locks_display') or job['locks']) or '-'} "
              f"timeout={human_duration(job.get('timeout'))} "
              "(hub profile differs from the local one)", file=sys.stderr)
    print(f"  submitter={submitter}" + (f" agent={agent}" if agent else ""))
    print(f"  sha={sha[:12]}{' (dirty)' if dirty else ''} "
          f"base={base[:12] if base else '-'} timeout={human_duration(timeout)}")
    if base_ref:
        print(f"  {base_ref}={submission['base_ref_sha'][:12] or '-'} "
              f"(baseline {submission['base_sha'][:12] or '-'})")
    if not args.wait:
        print(f"  follow: {_cli_prefix(args)} logs {job['id']} -f")
        return 0

    return _follow(client, job["id"], timeout=timeout + 120)


def _cli_prefix(args) -> str:
    prefix = "python -m calltable cli"
    config = getattr(args, "config", None)
    if config:
        prefix += f" --config {config}"
    return prefix


def _follow(client: HubClient, job_id: str, timeout: float) -> int:
    offset = 0
    deadline = time.time() + timeout
    job = None
    last_queue_print = 0.0
    while True:
        try:
            job = client.get_json(f"/api/jobs/{job_id}")
            if job.get("status") == "queued" and time.time() - last_queue_print >= 60:
                last_queue_print = time.time()
                print(f"queued {human_duration(job.get('queued_seconds', 0))}, "
                      f"{job.get('ahead', 0)} ahead", flush=True)
            payload, headers = client.request(
                "GET", f"/api/jobs/{job_id}/logs", params={"offset": offset},
                raw=True)
            if payload:
                sys.stdout.write(payload.decode("utf-8", "replace"))
                sys.stdout.flush()
                offset = int(headers.get("X-Calltable-Next", offset + len(payload)))
        except HubError as exc:
            print(f"\n[hub] {exc}", file=sys.stderr)
            return 1
        if job["status"] in ("succeeded", "failed", "cancelled", "timeout", "error"):
            # 收尾再拉一次，确保最后一块日志不丢
            payload, headers = client.request(
                "GET", f"/api/jobs/{job_id}/logs", params={"offset": offset},
                raw=True)
            if payload:
                sys.stdout.write(payload.decode("utf-8", "replace"))
                sys.stdout.flush()
            print(f"\n[{job['status']}] exit={job.get('exit_code')} "
                  f"duration={human_duration((job.get('finished') or time.time()) - job['started'] if job.get('started') else 0)}")
            if job.get("error"):
                print(f"error: {job['error']}", file=sys.stderr)
            return _exit_code(job)
        if time.time() > deadline:
            print("\n[calltable] local wait timed out; job may still be running",
                  file=sys.stderr)
            return 124
        time.sleep(0.7)


def _exit_code(job: dict) -> int:
    if job["status"] == "cancelled":
        return 130
    if job["status"] == "timeout":
        return 124
    code = job.get("exit_code")
    if code is None:
        return 1
    return int(code)


# ------------------------------------------------------------------ 其他命令

def cmd_ls(config: Config, args) -> int:
    client = _client(config, args)
    params = {"limit": args.limit}
    if args.running:
        params["status"] = "running"
    if args.queued:
        params["status"] = "queued"
    jobs = client.get_json("/api/jobs", params=params)["jobs"]
    if args.json:
        print(json.dumps(jobs, ensure_ascii=False, indent=2))
        return 0
    if not jobs:
        print("no jobs")
        return 0
    for job in jobs:
        print(_job_line(job))
    return 0


def cmd_show(config: Config, args) -> int:
    client = _client(config, args)
    job = client.get_json(f"/api/jobs/{args.id}")
    if args.json:
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0
    print(f"id        {job['id']}")
    group = job.get("group_name") or "-"
    print(f"project   {job['project']}   host {job['host']}   slot {job['slot']}   "
          f"group {group}")
    print(f"status    {job['status']}" +
          (f" (exit {job['exit_code']})" if job.get("exit_code") is not None else ""))
    print(f"submitter {job['submitter']}")
    print(f"agent     {job.get('agent') or '-'}")
    print(f"label     {job.get('label') or '-'}")
    print(f"sha       {job.get('sha') or '-'}{'  (dirty)' if job.get('dirty') else ''}")
    if job.get("base_ref"):
        print(f"base      {job['base_ref']} = "
              f"{(job.get('base_ref_sha') or '-')[:12]}"
              f"  (snapshot baseline {(job.get('base_sha') or '-')[:12]})")
    print(f"locks     "
          f"{', '.join(job.get('locks_display') or job.get('locks') or []) or '-'}")
    print(f"cmd       {' '.join(job.get('cmd') or [])}")
    print(f"created   {_fmt_time(job['created'])}")
    if job.get("status") == "queued":
        print(f"queued    {human_duration(job.get('queued_seconds', 0))} "
              f"({job.get('ahead', 0)} ahead of it for the same slot/locks)")
    print(f"started   {_fmt_time(job.get('started'))}")
    print(f"finished  {_fmt_time(job.get('finished'))}")
    if job.get("started"):
        end = job.get("finished") or time.time()
        print(f"duration  {human_duration(end - job['started'])}")
    print(f"peak      cpu={job.get('cpu_total_sec', 0):.3f}s "
          f"({job.get('cpu_peak_cores', 0):.2f} cores) "
          f"mem={_fmt_mem(job.get('mem_peak'))}")
    if job.get("error"):
        print(f"error     {job['error']}")
    artifacts = job.get("artifacts") or []
    if artifacts:
        print("artifacts")
        for art in artifacts:
            print(f"  {art['name']}  {_fmt_mem(art['size'])}  "
                  f"sha256={art['sha256'][:16]}…")
    return 0


def cmd_logs(config: Config, args) -> int:
    client = _client(config, args)
    offset = int(args.offset or 0)
    while True:
        job = client.get_json(f"/api/jobs/{args.id}")
        payload, headers = client.request(
            "GET", f"/api/jobs/{args.id}/logs", params={"offset": offset}, raw=True)
        if payload:
            sys.stdout.write(payload.decode("utf-8", "replace"))
            sys.stdout.flush()
            offset = int(headers.get("X-Calltable-Next", offset + len(payload)))
        terminal = job["status"] in ("succeeded", "failed", "cancelled",
                                     "timeout", "error")
        if not args.follow:
            return 0
        if terminal and not payload:
            return 0
        time.sleep(0.7)


def cmd_seed(config: Config, args) -> int:
    root = mirror_root_for(config, args.mirror_root)
    try:
        result = seed_mirror(args.project, args.source, root)
    except SeedError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 2
    print(f"seeded {result['project']} into {result['mirror']}")
    print(f"  source {result['source']}")
    print(f"  refs {len(result['refs'])} under refs/calltable/seed")
    print(f"  commits {result['objects_before']} -> {result['objects_after']}")
    return 0


def cmd_wait(config: Config, args) -> int:
    """重新挂到一个 detach 的任务上，并以它的退出码退出。"""
    client = _client(config, args)
    job = client.get_json(f"/api/jobs/{args.id}")
    timeout = float(job.get("timeout") or 3600) + 300
    return _follow(client, args.id, timeout=timeout)


def cmd_doctor(config: Config | None, args) -> int:
    from . import doctor

    hub = args.hub or (config.hub_url if config else DEFAULT_HUB_URL)
    token = args.token or (config.token_value if config else
                           os.environ.get("CALLTABLE_TOKEN", ""))
    owner = getattr(args, "tailnet", None) or (
        config.owner_tailnet if config else DEFAULT_OWNER_TAILNET)
    code, line, details = doctor.run(hub_url=hub, token=token,
                                     owner_tailnet=owner)
    for detail in details:
        print(detail, file=sys.stderr)
    print(line)
    return code


def cmd_setup(config: Config | None, args) -> int:
    from . import setup as setup_module

    try:
        path, data = setup_module.setup(
            hub=args.hub, token=args.token, owner_tailnet=args.tailnet,
            path=args.config, token_file=args.token_file)
    except setup_module.SetupError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {path}")
    print(f"hub   {data['hub_url']}")
    print("next: ct doctor")
    return 0


def cmd_self_update(config: Config | None, args) -> int:
    from . import setup as setup_module

    try:
        code, output = setup_module.self_update()
    except setup_module.SetupError as exc:
        print(f"self-update failed: {exc}", file=sys.stderr)
        return 2
    if output:
        print(output)
    return code


def cmd_cancel(config: Config, args) -> int:
    client = _client(config, args)
    job = client.post_json(f"/api/jobs/{args.id}/cancel", {})
    print(f"{job['id']} {job['status']}"
          + (" (cancel requested)" if job.get("cancel_requested") else ""))
    return 0


def cmd_hosts(config: Config, args) -> int:
    client = _client(config, args)
    state = client.get_json("/api/state")
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    groups = state.get("groups") or {}
    group_queue = state.get("group_queue") or {}
    for name, members in groups.items():
        queued = group_queue.get(name, 0)
        print(f"group {name:<10} -> {', '.join(members)}"
              + (f"   queued={queued}" if queued else ""))
    membership = {member: name for name, members in groups.items()
                  for member in members}
    for host in sorted(state["hosts"], key=lambda h: (not h["online"], h["name"])):
        alive = "online" if host["online"] else "offline"
        slot_text = " ".join(
            f"{name}:{host['used'].get(name, 0)}/{limit}"
            for name, limit in (host["slots"] or {}).items()) or "no-slots"
        locks = ",".join(host["locks"]) or "-"
        group = membership.get(host["name"], "-")
        flags = ""
        if host.get("draining"):
            flags += " DRAINING"
        if host.get("disk_low"):
            flags += " DISK_LOW"
        print(f"{host['name']:<10} {alive:<8} group={group:<8} "
              f"{slot_text:<28} locks={locks} "
              f"queue={host.get('queue', 0)}{flags}")
        system = host.get("system") or {}
        if system:
            detail = []
            if system.get("disk_free_gb") is not None:
                detail.append(f"disk_free={system['disk_free_gb']}G")
            if system.get("disk_runs_gb") is not None:
                prefix = "≥" if system.get("disk_runs_partial") else ""
                detail.append(f"runs={prefix}{system['disk_runs_gb']}G")
            if system.get("mem_free_gb"):
                detail.append(f"mem={system['mem_free_gb']}G/"
                              f"{system.get('mem_total_gb', 0)}G")
            if system.get("load1"):
                detail.append(f"load={system['load1']}")
            if system.get("disk_reason"):
                detail.append(f"disk_low_reason={system['disk_reason']}")
            if detail:
                print(f"    {' '.join(detail)}")
        for job in host["running"]:
            print(f"    > {job['id']} {job['project']} {job.get('label', '')}")
    if state["queued"]:
        print("\nqueued:")
        for job in state["queued"]:
            print(f"    {job['id']} {job['project']}@{job['host']} "
                  f"slot={job['slot']} "
                  f"locks={','.join(job.get('locks_display') or job['locks']) or '-'} "
                  f"waited={human_duration(job.get('queued_seconds', 0))} "
                  f"ahead={job.get('ahead', 0)} "
                  f"{job.get('label', '')}")
    return 0


def _set_drain(config: Config, args, drain: bool) -> int:
    client = _client(config, args)
    host = client.post_json(f"/api/hosts/{args.host}/drain", {"drain": drain})
    state = "drained" if host.get("draining") else "active"
    running = len(host.get("running") or [])
    print(f"{host['name']} {state}"
          + (f" (still running: {running})" if drain and running else ""))
    if drain and running:
        print("  wait for the running jobs to finish before restarting the "
              "executor")
    return 0


def cmd_drain(config: Config, args) -> int:
    return _set_drain(config, args, True)


def cmd_undrain(config: Config, args) -> int:
    return _set_drain(config, args, False)


def cmd_dashboard(config: Config, args) -> int:
    url = (args.hub or config.hub_url).rstrip("/")
    print(f"{url}/?token={args.token or config.token_value}")
    return 0


def _client(config: Config, args) -> HubClient:
    return HubClient(args.hub or config.hub_url, args.token or config.token_value)


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calltable",
                                     description="calltable CLI / hub / executor")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="mode", required=True)

    hub = sub.add_parser("hub", help="run the hub")
    hub.add_argument("--config")
    hub.add_argument("--bind")
    hub.add_argument("--port", type=int)

    ex = sub.add_parser("exec", help="run an executor on this host")
    ex.add_argument("--config")
    ex.add_argument("--host")
    ex.add_argument("--once", action="store_true", help="claim at most one job then exit")

    cli = sub.add_parser("cli", help="client commands (ct ...)")
    cli.add_argument("--version", action="version", version=__version__)
    csub = cli.add_subparsers(dest="command_name", required=True)

    run = csub.add_parser("run", help="snapshot the worktree and submit a job")
    _add_cli_options(run)
    run.add_argument("--host",
                     help="target host; omitted => inferred from project/platform")
    run.add_argument("--platform", choices=["linux", "windows", "macos"],
                     help="platform hint used to pick a host from the profiles")
    run.add_argument("--project", required=True)
    run.add_argument("--slot")
    run.add_argument("--lock", action="append", default=[],
                     help="named lock (repeatable); profile/rule locks are added")
    run.add_argument("--no-lock", action="store_true",
                     help="drop the locks that the profile/rules add")
    run.add_argument("--dry-run", action="store_true",
                     help="print the effective slot/locks/timeout and exit")
    run.add_argument("--timeout", help="e.g. 3h, 90m, 3600")
    run.add_argument("--base-ref", default="origin/main",
                     help="baseline ref shipped into the checkout (default origin/main)")
    run.add_argument("--no-base-ref", action="store_true",
                     help="do not ship any baseline ref")
    run.add_argument("--yes", action="store_true",
                     help="upload even when the bundle is larger than 20MB")
    run.add_argument("--wait", action="store_true", help="stream logs and exit with job code")
    run.add_argument("--detach", action="store_true", help="return immediately (default)")
    run.add_argument("--label", default="")
    run.add_argument("--submitter", default="")
    run.add_argument("--agent", default="",
                     help="agent/session name (or CALLTABLE_AGENT); shown on the board")
    run.add_argument("--repo", help="git repository (default: cwd)")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="-- <command...>")
    run.set_defaults(func=cmd_run)

    ls = csub.add_parser("ls", help="list jobs")
    _add_cli_options(ls)
    ls.add_argument("--running", action="store_true")
    ls.add_argument("--queued", action="store_true")
    ls.add_argument("--limit", type=int, default=30)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    show = csub.add_parser("show", help="show one job")
    _add_cli_options(show)
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    logs = csub.add_parser("logs", help="print logs")
    _add_cli_options(logs)
    logs.add_argument("id")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--offset", type=int, default=0)
    logs.set_defaults(func=cmd_logs)

    seed = csub.add_parser("seed", help="pre-fill a local mirror from a checkout")
    _add_cli_options(seed)
    seed.add_argument("--project", required=True)
    seed.add_argument("--from", dest="source", required=True,
                      help="path to a git checkout to fetch from")
    seed.add_argument("--mirror-root",
                      help="override the mirror directory (hub: data/mirrors, "
                           "executor: mirror_root)")
    seed.set_defaults(func=cmd_seed)

    wait = csub.add_parser("wait", help="re-attach to a job and exit with its code")
    _add_cli_options(wait)
    wait.add_argument("id")
    wait.set_defaults(func=cmd_wait)

    doctor = csub.add_parser("doctor", help="check this machine can use calltable")
    _add_cli_options(doctor)
    doctor.add_argument("--tailnet", help="owner tailnet name")
    doctor.set_defaults(func=cmd_doctor)

    setup = csub.add_parser("setup", help="write the client config")
    _add_cli_options(setup)
    setup.add_argument("--tailnet", help="owner tailnet name")
    setup.add_argument("--token-file",
                       help="read the token from a local file (no ssh needed)")
    setup.set_defaults(func=cmd_setup)

    update = csub.add_parser("self-update", help="git pull the installed checkout")
    _add_cli_options(update)
    update.set_defaults(func=cmd_self_update)

    cancel = csub.add_parser("cancel", help="cancel a job")
    _add_cli_options(cancel)
    cancel.add_argument("id")
    cancel.set_defaults(func=cmd_cancel)

    drain = csub.add_parser("drain", help="stop dispatching new jobs to a host")
    _add_cli_options(drain)
    drain.add_argument("host")
    drain.set_defaults(func=cmd_drain)

    undrain = csub.add_parser("undrain", help="resume dispatching to a host")
    _add_cli_options(undrain)
    undrain.add_argument("host")
    undrain.set_defaults(func=cmd_undrain)

    hosts = csub.add_parser("hosts", help="hosts, slots and locks")
    _add_cli_options(hosts)
    hosts.add_argument("--json", action="store_true")
    hosts.set_defaults(func=cmd_hosts)

    dash = csub.add_parser("dashboard", help="print the dashboard URL with token")
    _add_cli_options(dash)
    dash.set_defaults(func=cmd_dashboard)
    return parser


def _add_cli_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config")
    parser.add_argument("--hub", help="override hub URL")
    parser.add_argument("--token", help="override token")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # 管理命令也支持 `calltable seed ...`（等价于 `calltable cli seed ...`）。
    if argv and argv[0] == "seed":
        argv = ["cli", *argv]
    # Windows 控制台常见 GBK 代码页；日志与表格里可能有非 GBK 字符，宁可替换也不要崩。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    name = getattr(args, "command_name", None)

    # setup 不需要已有配置；doctor/self-update 允许没有配置也能跑。
    if name == "setup":
        return int(cmd_setup(None, args) or 0)
    if name == "self-update":
        return int(cmd_self_update(None, args) or 0)
    try:
        config = load_config(getattr(args, "config", None),
                             require_token=(name not in ("doctor", "seed")))
    except ConfigError as exc:
        if name == "doctor":
            print(f"config: {exc}", file=sys.stderr)
            config = None
        else:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
    if args.mode == "hub":
        from . import hub as hub_module
        if getattr(args, "bind", None):
            config.raw["hub_bind"] = args.bind
        if getattr(args, "port", None):
            config.raw["hub_port"] = args.port
        hub_module.serve(config)
        return 0
    if args.mode == "exec":
        from . import executor as exec_module
        if getattr(args, "host", None):
            config.raw["exec_host"] = args.host
        exec_module.serve(config, once=bool(getattr(args, "once", False)))
        return 0
    # cli
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if args.command_name == "run" and not args.command:
        print("missing command; usage: ct run ... -- <command...>", file=sys.stderr)
        return 2
    return int(args.func(config, args) or 0)
