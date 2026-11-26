"""仓库自带的项目声明 ``.calltable.json``（issue #3）。

放在 git 仓库根目录，``ct run`` 在仓库里运行时自动读取，于是不用每次写
``--project`` / ``--host``::

    {
      "project": "demo",
      "host": "linux",
      "slot": "light",
      "timeout": "30m",
      "artifacts": ["reports/*.xml"],
      "rules": [{"match": "integration", "slot": "heavy", "timeout": "2h"}],
      "tasks": {"verify": ["./build/verify.sh"]}
    }

优先级：命令行参数 > 仓库声明 > hub 侧 profile。``slot`` / ``locks`` /
``timeout`` / ``rules`` 的含义和 profile 一样（规则用 :class:`Rule` 解析）；
和主机有关的字段（``env_files``、``env``、``setup``、内存/CPU 限制等）只能写在
hub 侧 profile 里，出现在声明中直接报错并说明原因。

``ct run <任务名>``：命令里没有 ``--``、只有一个词、且这个词是声明里的任务名
时按任务展开；其余情况（包括 ``ct run -- <命令>``）都按命令原样提交。
例外：没有 ``--``、第一个词是任务名、后面又有以 ``-`` 开头的词时，``ct run``
报错（选项要写在任务名前面），见 ``cli._prepare_run``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .profiles import Profile, Resolution, Rule, match_rule
from .util import (MAX_ARTIFACT_PATTERNS, artifact_pattern_problem,
                   parse_duration, valid_project_name)

DECLARATION_FILE = ".calltable.json"
#: 声明文件的大小上限：先看大小再读，免得把超大文件整个读进内存。
MAX_DECLARATION_BYTES = 1024 * 1024
PLATFORMS = ("linux", "windows", "macos")
#: 和 hub 校验锁名用的同一个模式（``hub.create_job``）。
LOCK_RE = re.compile(r"[A-Za-z0-9._{}-]+(@[A-Za-z0-9._{}-]+)?")
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")

ALLOWED_FIELDS = ("$schema", "project", "host", "platform", "slot", "locks",
                  "timeout", "artifacts", "rules", "tasks")
RULE_FIELDS = ("match", "slot", "locks", "timeout")

_HOST_ONLY_REASON = ("it depends on the executor host, so it may only be set "
                     "in the hub-side profile <project>@<host>.json")
#: 只允许 hub 侧 profile 设置的字段 -> 拒绝原因。
HOST_ONLY_FIELDS = {
    "env_files": "env_files are paths on the executor host; " + _HOST_ONLY_REASON,
    "env": "env is part of the host environment; " + _HOST_ONLY_REASON
           + " (set variables in the command or a task script instead)",
    "setup": "setup commands prepare the host checkout; " + _HOST_ONLY_REASON,
    "memory_max": "memory limits are a host resource policy; " + _HOST_ONLY_REASON,
    "cpu_weight": "CPU weights are a host resource policy; " + _HOST_ONLY_REASON,
    "slice": "systemd slices exist on the host; " + _HOST_ONLY_REASON,
    "cloudrun": "the Cloud Run image is part of the provider host; "
                + _HOST_ONLY_REASON,
    "unsupported": "marking a project unsupported on a host is a hub decision; "
                   + _HOST_ONLY_REASON,
    "note": "note belongs to an unsupported hub-side profile; " + _HOST_ONLY_REASON,
    "name": "name is the key of a hub-side profile <project>@<host>; use "
            "\"project\" (and \"host\") in the declaration",
}


class DeclarationError(ValueError):
    """声明文件有错；``field`` 是出错的字段路径（如 ``rules[1].timeout``）。"""

    def __init__(self, path: Path, field_name: str, message: str):
        self.path = Path(path)
        self.field = field_name
        where = f"field {field_name!r}: " if field_name else ""
        super().__init__(f"{self.path}: {where}{message}")


@dataclass
class Declaration:
    path: Path
    project: str | None = None
    host: str | None = None
    platform: str | None = None
    slot: str | None = None
    #: None = 没写，沿用 hub profile；``[]`` = 显式不加锁。
    locks: list[str] | None = None
    timeout: int | None = None
    #: None = 没写，沿用 hub profile 的产物 glob。
    artifacts: list[str] | None = None
    rules: list[Rule] = field(default_factory=list)
    tasks: dict[str, list[str]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return DECLARATION_FILE


# ------------------------------------------------------------------ 解析

def _string(path: Path, name: str, value, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DeclarationError(path, name, f"must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise DeclarationError(path, name, "must not be empty")
    return value.strip()


def _string_list(path: Path, name: str, value) -> list[str]:
    if not isinstance(value, list):
        raise DeclarationError(path, name,
                               f"must be an array of strings, got {type(value).__name__}")
    result = []
    for index, item in enumerate(value):
        result.append(_string(path, f"{name}[{index}]", item))
    return result


def _locks(path: Path, name: str, value) -> list[str]:
    locks = _string_list(path, name, value)
    for index, lock in enumerate(locks):
        if not LOCK_RE.fullmatch(lock):
            raise DeclarationError(path, f"{name}[{index}]", f"bad lock name: {lock!r}")
    if len(set(locks)) != len(locks):
        raise DeclarationError(path, name, "duplicate locks")
    return locks


def _timeout(path: Path, name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DeclarationError(path, name, "must be a duration like \"90m\", \"3h\" or seconds")
    try:
        seconds = parse_duration(value)
    except (ValueError, OverflowError) as exc:
        # OverflowError：JSON 里的 1e400 读成 inf，int() 转不了。
        raise DeclarationError(path, name, str(exc)) from None
    if seconds <= 0:
        raise DeclarationError(path, name, "must be positive")
    return seconds


def _reject_unknown(path: Path, prefix: str, data: dict, allowed,
                    host_only: dict[str, str]) -> None:
    for key in data:
        name = f"{prefix}{key}"
        if key in host_only:
            raise DeclarationError(path, name, f"not allowed in {DECLARATION_FILE}: "
                                               f"{host_only[key]}")
        if key not in allowed:
            raise DeclarationError(path, name, "unknown field (allowed: "
                                               + ", ".join(allowed) + ")")


def _rule(path: Path, index: int, data) -> Rule:
    prefix = f"rules[{index}]"
    if not isinstance(data, dict):
        raise DeclarationError(path, prefix, "must be an object")
    _reject_unknown(path, prefix + ".", data, RULE_FIELDS,
                    {"memory_max": HOST_ONLY_FIELDS["memory_max"]})
    if "match" not in data:
        raise DeclarationError(path, prefix + ".match", "is required")
    match = _string(path, prefix + ".match", data["match"])
    try:
        re.compile(match)
    except re.error as exc:
        raise DeclarationError(path, prefix + ".match", f"invalid regex: {exc}") from None
    checked: dict = {"match": match}
    if "slot" in data:
        checked["slot"] = _string(path, prefix + ".slot", data["slot"])
    if "locks" in data:
        checked["locks"] = _locks(path, prefix + ".locks", data["locks"])
    if "timeout" in data:
        checked["timeout"] = _timeout(path, prefix + ".timeout", data["timeout"])
    # 校验过后交给 profile 的同一套解析，字段含义完全一致。
    return Rule.from_dict(checked)


def parse_declaration(data, path: Path) -> Declaration:
    path = Path(path)
    if not isinstance(data, dict):
        raise DeclarationError(path, "", "must be a JSON object")
    _reject_unknown(path, "", data, ALLOWED_FIELDS, HOST_ONLY_FIELDS)
    decl = Declaration(path=path)
    if "project" in data:
        project = _string(path, "project", data["project"])
        if not valid_project_name(project):
            raise DeclarationError(path, "project", f"invalid project name: {project!r}")
        decl.project = project
    if "host" in data and "platform" in data:
        raise DeclarationError(path, "platform", "give either \"host\" or \"platform\", not both")
    if "host" in data:
        decl.host = _string(path, "host", data["host"])
    if "platform" in data:
        platform = _string(path, "platform", data["platform"]).lower()
        if platform not in PLATFORMS:
            raise DeclarationError(path, "platform",
                                   f"must be one of {', '.join(PLATFORMS)}, got {platform!r}")
        decl.platform = platform
    if "slot" in data:
        decl.slot = _string(path, "slot", data["slot"])
    if "locks" in data:
        decl.locks = _locks(path, "locks", data["locks"])
    if "timeout" in data:
        decl.timeout = _timeout(path, "timeout", data["timeout"])
    if "artifacts" in data:
        artifacts = _string_list(path, "artifacts", data["artifacts"])
        # 和 hub 同一套上限，免得快照、上传之后才被 hub 拒绝。
        if len(artifacts) > MAX_ARTIFACT_PATTERNS:
            raise DeclarationError(path, "artifacts",
                                   f"at most {MAX_ARTIFACT_PATTERNS} patterns, "
                                   f"got {len(artifacts)}")
        for index, pattern in enumerate(artifacts):
            problem = artifact_pattern_problem(pattern)
            if problem:
                raise DeclarationError(path, f"artifacts[{index}]", problem)
        decl.artifacts = artifacts
    if "rules" in data:
        if not isinstance(data["rules"], list):
            raise DeclarationError(path, "rules", "must be an array of rule objects")
        decl.rules = [_rule(path, index, item)
                      for index, item in enumerate(data["rules"])]
    if "tasks" in data:
        tasks = data["tasks"]
        if not isinstance(tasks, dict):
            raise DeclarationError(path, "tasks", "must be an object of name -> command array")
        for name, command in tasks.items():
            key = f"tasks.{name}"
            if not TASK_RE.fullmatch(name):
                raise DeclarationError(path, key, "task names use letters, digits and "
                                                  "._:- and must not start with '-'")
            argv = _string_list(path, key, command)
            if not argv:
                raise DeclarationError(path, key, "must be a non-empty command array")
            decl.tasks[name] = argv
    return decl


def find_declaration(repo: str | Path = ".") -> Path | None:
    """git 仓库根目录下的声明文件；不在仓库里或没有文件时返回 None。"""
    try:
        root = gitops.repo_root(repo)
    except (gitops.GitError, OSError):
        return None
    path = root / DECLARATION_FILE
    return path if path.is_file() else None


def load_declaration(repo: str | Path = ".") -> Declaration | None:
    path = find_declaration(repo)
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DeclarationError(path, "", f"cannot read: {exc}") from None
    if size > MAX_DECLARATION_BYTES:
        raise DeclarationError(path, "", f"file is too large ({size} bytes, "
                                         f"at most {MAX_DECLARATION_BYTES})")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DeclarationError(path, "", f"not valid UTF-8: {exc}") from None
    except OSError as exc:
        raise DeclarationError(path, "", f"cannot read: {exc}") from None
    try:
        data = json.loads(text)
    except RecursionError:
        raise DeclarationError(path, "", "invalid JSON: nested too deeply") from None
    except ValueError as exc:
        raise DeclarationError(path, "", f"invalid JSON: {exc}") from None
    return parse_declaration(data, path)


# ------------------------------------------------------------------ 生效值

def expand_task(decl: Declaration | None, command: list[str],
                separator: bool) -> tuple[list[str], str]:
    """``ct run <任务名>`` -> (任务命令, 任务名)；不是任务时原样返回 (command, "")。"""
    if decl is None or separator or len(command) != 1:
        return command, ""
    name = command[0]
    if name in decl.tasks:
        return list(decl.tasks[name]), name
    return command, ""


def choose_host(project: str, cli_host: str | None, cli_platform: str | None,
                decl: Declaration | None, profiles: dict[str, Profile],
                resolve_host) -> tuple[str, str]:
    """返回 (主机或主机组, 来源)。命令行 > 声明 > profile 推断。"""
    if cli_host:
        return cli_host, "--host"
    if cli_platform:
        return (resolve_host(project, None, cli_platform, profiles),
                f"--platform {cli_platform} + hub profiles")
    if decl is not None and decl.host:
        return decl.host, f"{DECLARATION_FILE} host"
    if decl is not None and decl.platform:
        return (resolve_host(project, None, decl.platform, profiles),
                f"{DECLARATION_FILE} platform {decl.platform} + hub profiles")
    return resolve_host(project, None, None, profiles), "inferred from hub profiles"


@dataclass
class Submitted:
    """要随提交发给 hub 的「显式值」以及每个值的来源。

    hub 把显式值当成命令行参数处理（覆盖它自己的 profile/规则），所以声明里的
    值以显式值的形式送上去；没被命令行或声明设置的字段仍由 hub profile 决定。
    """

    slot: str | None
    locks: list[str]
    no_lock: bool
    timeout: int | None
    sources: dict[str, str]
    repo_rule: Rule | None = None


def explicit_values(decl: Declaration | None, command: list[str], *,
                    slot: str | None, locks: list[str], timeout: int | None,
                    no_lock: bool) -> Submitted:
    sources: dict[str, str] = {}
    cli_locks = list(locks or [])
    if decl is None:
        if slot:
            sources["slot"] = "--slot"
        if timeout is not None:
            sources["timeout"] = "--timeout"
        if no_lock:
            sources["locks"] = "--no-lock" + (" + --lock" if cli_locks else "")
        elif cli_locks:
            sources["locks"] = "hub profile + --lock"
        return Submitted(slot or None, cli_locks, bool(no_lock), timeout, sources)

    holder = Profile(name=DECLARATION_FILE, project=decl.project or "", host="",
                     rules=decl.rules)
    rule, _ = match_rule(holder, command)
    where_rule = (f"{DECLARATION_FILE} rule {rule.match!r}" if rule is not None else "")

    repo_slot, slot_src = decl.slot, f"{DECLARATION_FILE} slot"
    if rule is not None and rule.slot:
        repo_slot, slot_src = rule.slot, where_rule
    repo_timeout, timeout_src = decl.timeout, f"{DECLARATION_FILE} timeout"
    if rule is not None and rule.timeout:
        repo_timeout, timeout_src = rule.timeout, where_rule
    repo_locks, locks_src = decl.locks, f"{DECLARATION_FILE} locks"
    if rule is not None and rule.locks is not None:
        repo_locks, locks_src = rule.locks, where_rule

    if slot:
        final_slot = slot
        sources["slot"] = "--slot"
    else:
        final_slot = repo_slot
        if repo_slot:
            sources["slot"] = slot_src
    if timeout is not None:
        final_timeout = timeout
        sources["timeout"] = "--timeout"
    else:
        final_timeout = repo_timeout
        if repo_timeout is not None:
            sources["timeout"] = timeout_src
    if no_lock:
        final_locks, final_no_lock = cli_locks, True
        sources["locks"] = "--no-lock" + (" + --lock" if cli_locks else "")
    elif repo_locks is not None:
        # 声明的锁取代 hub profile 的默认锁：用 no_lock 去掉 hub 侧默认值，
        # 再把声明的锁当显式值送上去。
        final_locks = list(repo_locks)
        for lock in cli_locks:
            if lock not in final_locks:
                final_locks.append(lock)
        final_no_lock = True
        sources["locks"] = locks_src + (" + --lock" if cli_locks else "")
    else:
        final_locks, final_no_lock = cli_locks, False
        if cli_locks:
            sources["locks"] = "hub profile + --lock"
    return Submitted(final_slot or None, final_locks, final_no_lock,
                     final_timeout, sources, rule)


def fallback_source(resolution: Resolution, profile: Profile | None) -> str:
    """没被命令行或声明设置的值：由 hub 侧 profile（或 hub 默认值）决定。"""
    if profile is None:
        return "hub default (no local copy of a hub profile)"
    if resolution.rule is not None:
        return f"hub profile {profile.name} rule {resolution.rule.match!r}"
    return f"hub profile {profile.name}"
