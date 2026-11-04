"""``calltable seed``：把本机仓库的对象预填进镜像，减少跨慢链路的 bundle。

镜像里对象一多，客户端 ``ct run`` 就只传增量（hub 的 /have 按对象是否存在
判断）；执行器本地缺提交时也优先从 hub 拉增量。

命令可重复执行：``git fetch`` 到固定的 ``refs/calltable/seed/*`` 命名空间，不会改动
源仓库，也不会删除镜像里已有的对象。
"""

from __future__ import annotations

from pathlib import Path

from . import gitops
from .config import Config, ConfigError
from .util import valid_project_name

REFSPECS = [
    "+refs/heads/*:refs/calltable/seed/heads/*",
    "+refs/remotes/origin/*:refs/calltable/seed/origin/*",
]


class SeedError(RuntimeError):
    pass


def mirror_root_for(config: Config, override: str | None = None) -> Path:
    """hub 用 ``data/mirrors``，执行器用配置里的 ``mirror_root``。"""
    if override:
        return Path(override).expanduser()
    try:
        config.exec_host  # noqa: B018 - 仅用于判断当前配置是不是执行器
        return config.mirror_root
    except ConfigError:
        return config.data_dir / "mirrors"


def seed(project: str, source: str | Path, mirror_root: str | Path) -> dict:
    if not valid_project_name(project):
        raise SeedError(f"invalid project name: {project!r}")
    source = Path(source).expanduser().resolve()
    if not (source / ".git").exists() and not (source / "HEAD").exists():
        raise SeedError(f"{source} is not a git repository")
    mirror = gitops.ensure_mirror(Path(mirror_root).expanduser() / f"{project}.git")

    before = _count_objects(mirror)
    proc = gitops.git(["-C", str(mirror), "fetch", "--no-tags", str(source),
                       *REFSPECS], check=False)
    if proc.returncode != 0:
        raise SeedError(
            f"git fetch from {source} failed: {(proc.stderr or '').strip()}"
        )
    after = _count_objects(mirror)
    refs = gitops.git(["-C", str(mirror), "for-each-ref",
                       "--format=%(refname)", "refs/calltable/seed/"], check=False)
    return {
        "project": project,
        "mirror": str(mirror),
        "source": str(source),
        "objects_before": before,
        "objects_after": after,
        "refs": [line for line in refs.stdout.splitlines() if line],
    }


def _count_objects(mirror: Path) -> int:
    proc = gitops.git(["-C", str(mirror), "rev-list", "--all", "--count"],
                      check=False)
    try:
        return int(proc.stdout.strip())
    except (TypeError, ValueError):
        return 0
