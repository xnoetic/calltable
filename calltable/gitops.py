"""Git 相关操作：快照提交、增量 bundle、镜像与 worktree。

只依赖 git 命令行，不引入任何库。

设计要点
--------
* ``snapshot_commit`` 用临时 ``GIT_INDEX_FILE``（外加 ``git write-tree`` /
  ``commit-tree``）把工作区里**未提交、未跟踪且未被忽略**的改动做成一个
  独立提交；用户的 ``.git/index`` 与工作区文件都不被触碰。
* bundle 用一次性 ref ``refs/calltable/snapshots/<random>`` 打包，打完立刻删除，
  所以也不会在仓库里留下长期引用。
* 增量 bundle 用 ``<ref> --not <base>``，前提是 hub 镜像已有 ``base``；
  hub 侧 ``ingest_bundle`` 会把快照 ref 归并成 ``refs/calltable/keep/<sha>``，
  避免对象因为快照 ref 被删而变成不可达。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


#: 跨平台一致性：不让 autocrlf 改写 blob，长路径在 Windows 上可用。
GIT_CONFIG_OPTS = ["-c", "core.autocrlf=false", "-c", "core.longpaths=true",
                   "-c", "core.quotepath=false"]


def git(args: list[str], cwd: str | Path | None = None,
        env: dict | None = None, check: bool = True,
        input_data: bytes | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", *GIT_CONFIG_OPTS, *args], cwd=str(cwd) if cwd else None,
        env=full_env, input=input_data, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}",
            proc.stdout, proc.stderr)
    return proc


def _identity_env(repo: Path) -> dict:
    env = {}
    email = git(["config", "user.email"], cwd=repo, check=False).stdout.strip()
    name = git(["config", "user.name"], cwd=repo, check=False).stdout.strip()
    if not email:
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "calltable@localhost"
    if not name:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "calltable"
    return env


def repo_root(path: str | Path = ".") -> Path:
    proc = git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    if proc.returncode != 0:
        raise GitError(f"not a git repository: {path}")
    return Path(proc.stdout.strip())


def rev_parse(repo: Path, rev: str) -> str | None:
    proc = git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
               cwd=repo, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_dirty(repo: Path) -> bool:
    """工作区/index 是否有任何改动（含未跟踪、未忽略文件）。"""
    out = git(["status", "--porcelain", "--untracked-files=normal"], cwd=repo).stdout
    return bool(out.strip())


def snapshot_commit(repo: Path, message: str = "calltable snapshot") -> tuple[str, bool]:
    """把当前工作区快照成一个提交，返回 (sha, 是否已有 HEAD)。

    不修改 .git/index，也不修改任何工作区文件。
    """
    repo = repo_root(repo)
    tmpdir = Path(tempfile.mkdtemp(prefix="calltable-snapshot-"))
    index = tmpdir / "index"
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        head = rev_parse(repo, "HEAD")
        if head:
            git(["read-tree", "HEAD"], cwd=repo, env=env)
        else:
            git(["read-tree", "--empty"], cwd=repo, env=env)
        # -A：新增/修改/删除；被 .gitignore 忽略的文件不会进来。
        git(["add", "-A", "--", "."], cwd=repo, env=env)
        tree = git(["write-tree"], cwd=repo, env=env).stdout.strip()
        args = ["commit-tree", tree]
        if head:
            args += ["-p", head]
        args += ["-m", message]
        sha = git(args, cwd=repo, env={**env, **_identity_env(repo)}).stdout.strip()
        return sha, bool(head)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def candidates(repo: Path, limit: int = 200) -> list[str]:
    """本地可能已被 hub 拥有的提交，按从新到旧。"""
    out = git(["rev-list", f"--max-count={limit}", "HEAD"], cwd=repo, check=False)
    return [line for line in out.stdout.splitlines() if line.strip()]


def create_bundle(repo: Path, sha: str, base: str | None, out_path: Path) -> bool:
    """生成 sha（相对 base）的 bundle。返回 False 表示没有增量（sha == base）。"""
    if base == sha:
        return False
    return create_bundle_multi(repo, [(sha, "snapshots")], [base] if base else [],
                               out_path)


def newest_common(repo: Path, tip: str, hub_commits: set[str],
                  depth: int = 20000) -> str | None:
    """tip 的历史上最新的、hub 已经有的提交。

    用它作为 ``--not`` 的排除点，bundle 只带增量。不要用「祖先链完整」那种
    更严格的判定：镜像可能来自部分 checkout，祖先链有洞，但排除点本身存在
    就足以让 fetch 校验通过（bundles 只要求列出的前置提交存在）。
    """
    out = git(["rev-list", f"--max-count={depth}", tip], cwd=repo, check=False)
    for line in out.stdout.splitlines():
        commit = line.strip()
        if commit and commit in hub_commits:
            return commit
    return None


def create_bundle_multi(repo: Path, tips: list[tuple[str, str]],
                        excludes: list[str], out_path: Path) -> bool:
    """把多个提交作为独立 tip 打进一个 bundle（可选排除已有提交）。

    ``tips`` 是 ``(sha, 分类名)``；每个 tip 用一次性 ref
    ``refs/calltable/<分类>/<uuid>`` 打包，打完立刻删除，不在仓库里留引用。
    """
    repo = repo_root(repo)
    refs: list[str] = []
    try:
        for sha, kind in tips:
            ref = f"refs/calltable/{kind}/{uuid.uuid4().hex}"
            git(["update-ref", ref, sha], cwd=repo)
            refs.append(ref)
        args = ["bundle", "create", str(out_path), *refs]
        cleaned = [sha for sha in excludes if sha and sha not in
                   {tip_sha for tip_sha, _ in tips}]
        if cleaned:
            args += ["--not", *cleaned]
        git(args, cwd=repo)
        return True
    finally:
        for ref in refs:
            git(["update-ref", "-d", ref], cwd=repo, check=False)


def bundle_prerequisites(path: Path) -> list[str]:
    """读 bundle 声明的前置提交（为了测试与诊断）。"""
    out = git(["bundle", "list-heads", str(path)], check=False)
    if out.returncode != 0:
        raise GitError(f"not a bundle: {path}: {out.stderr.strip()}")
    # git bundle list-heads 只列 refs；前置提交要解析 pack 头部的注释。
    with open(path, "rb") as handle:
        head = handle.read(4096).decode("utf-8", "replace")
    result = []
    for line in head.splitlines():
        if line.startswith("-"):
            token = line[1:].split()
            if token:
                result.append(token[0])
    return result


# --------------------------------------------------------------- 镜像与检出

def ensure_mirror(mirror: str | Path) -> Path:
    mirror = Path(mirror)
    if (mirror / "HEAD").exists():
        return mirror
    mirror.mkdir(parents=True, exist_ok=True)
    git(["init", "--bare", "-q", str(mirror)])
    return mirror


def has_commit(mirror: str | Path, sha: str) -> bool:
    proc = git(["-C", str(mirror), "cat-file", "-e", f"{sha}^{{commit}}"], check=False)
    return proc.returncode == 0


def have_commits(mirror: str | Path, shas: list[str]) -> list[str]:
    """一次 ``git cat-file --batch-check`` 判定多个提交是否存在（I9）。

    输入按行喂给 git，输出每行形如 ``<sha> commit <size>`` 或 ``<sha> missing``。
    """
    wanted = [sha for sha in shas if sha and all(c in "0123456789abcdef" for c in sha.lower())]
    if not wanted or not (Path(mirror) / "HEAD").exists():
        return []
    payload = chr(10).join(f"{sha}^{{commit}}" for sha in wanted) + chr(10)
    proc = git(["-C", str(mirror), "cat-file", "--batch-check"],
               input_data=payload, check=False)
    if proc.returncode != 0:
        return []
    present = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit":
            present.add(parts[0])
    return [sha for sha in wanted if sha in present]


def keep_refs(mirror: str | Path) -> list[dict]:
    """列出 refs/calltable/keep/*，带提交时间（unix 秒）。"""
    proc = git(["-C", str(mirror), "for-each-ref",
                "--format=%(refname) %(objectname) %(committerdate:unix)",
                "refs/calltable/keep/"], check=False)
    result = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                result.append({"ref": parts[0], "sha": parts[1],
                               "committed": int(parts[2])})
            except ValueError:
                continue
    return result


def prune_keep_refs(mirror: str | Path, older_than: float) -> int:
    removed = 0
    for entry in keep_refs(mirror):
        if entry["committed"] < older_than:
            git(["-C", str(mirror), "update-ref", "-d", entry["ref"]], check=False)
            removed += 1
    return removed


def ingest_bundle(mirror: str | Path, bundle: str | Path) -> list[dict]:
    """把 bundle fetch 进镜像；快照 ref 收束为 refs/calltable/keep/<sha>。

    返回 ``[{"ref": ..., "sha": ...}]``。
    """
    mirror = ensure_mirror(mirror)
    git(["-C", str(mirror), "fetch", "--no-tags", str(bundle),
         "+refs/*:refs/mirror/*"])
    heads = git(["-C", str(mirror), "for-each-ref",
                 "--format=%(refname) %(objectname)", "refs/mirror/"]).stdout
    kept: list[dict] = []
    for line in heads.splitlines():
        ref, _, sha = line.partition(" ")
        if not sha:
            continue
        kept.append({"ref": ref, "sha": sha})
        # 所有一次性 ref（快照、基线）都收束成 refs/calltable/keep/<sha>，
        # 保证对象可达，并且 hub 之后还能为任意 sha 现打 thick bundle。
        if ref.startswith("refs/mirror/calltable/"):
            git(["-C", str(mirror), "update-ref", f"refs/calltable/keep/{sha}", sha])
            git(["-C", str(mirror), "update-ref", "-d", ref], check=False)
    return kept


def worktree_add(mirror: str | Path, path: str | Path, sha: str) -> Path:
    path = Path(path)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["-C", str(mirror), "worktree", "add", "--detach", "--force", str(path), sha])
    return path


def worktree_remove(mirror: str | Path, path: str | Path) -> None:
    git(["-C", str(mirror), "worktree", "remove", "--force", str(path)], check=False)
    shutil.rmtree(str(path), ignore_errors=True)
    git(["-C", str(mirror), "worktree", "prune"], check=False)


def clone_shared(mirror: str | Path, path: str | Path, sha: str) -> Path:
    """每个任务独立的检出：``git clone --shared --no-checkout`` + detach checkout。

    不用 ``git worktree``：worktree 与镜像共享 refs，两个并发任务各自的
    ``refs/remotes/origin/main`` 会互相覆盖。clone 出来的仓库有独立 refs。
    """
    path = Path(path)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--shared", "--no-checkout", "-q", str(mirror), str(path)])
    git(["-C", str(path), "checkout", "--detach", "-q", sha])
    return path


def remote_ref_name(base_ref: str) -> str:
    """``origin/main`` → ``refs/remotes/origin/main``；``refs/...`` 原样保留。"""
    if base_ref.startswith("refs/"):
        return base_ref
    if base_ref.startswith("origin/"):
        return "refs/remotes/" + base_ref
    return "refs/remotes/origin/" + base_ref


def set_base_ref(checkout: str | Path, base_ref: str, sha: str) -> str:
    """在任务检出里把 ``origin/main`` 指向本次提交的基线 sha。

    同时写 ``refs/remotes/origin/HEAD``，让 ``git rev-parse origin/main`` 与
    ``git merge-base HEAD origin/main`` 都能正常工作；``origin`` remote 在
    clone 时已指向本机镜像，因此 ``git fetch origin`` 也是离线可用的。
    """
    ref = remote_ref_name(base_ref)
    git(["-C", str(checkout), "update-ref", ref, sha])
    if ref.startswith("refs/remotes/origin/"):
        try:
            git(["-C", str(checkout), "symbolic-ref",
                 "refs/remotes/origin/HEAD", ref], check=False)
        except GitError:
            pass
    return ref
