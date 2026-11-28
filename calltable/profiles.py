"""项目 profile：环境、槽位、锁、规则、产物 glob。

文件位于 ``profiles/<project>@<host>.json``，也支持 ``<project>@*.json``
（任意主机）、``*@<host>.json``（该主机任意项目）与 ``*@*.json``；只取最具体
的一个：``project@host > project@* > *@host > *@*``。Windows 文件名不能含
``*``，通配写在 JSON 的 ``name`` 字段里（文件名随意）。字段见 README。

``rules`` 按命令类型给默认值：每条规则的 ``match`` 是正则，匹配用
``shlex.join`` 拼好的命令；按顺序取第一条命中的规则，一条都不命中时
用 profile 自身的默认值。命令行里显式写的 ``--slot`` / ``--lock`` /
``--timeout`` 始终优先，``--no-lock`` 去掉规则/ profile 的默认锁。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .util import parse_duration


@dataclass
class Rule:
    """按命令匹配的一条默认值规则。

    ``locks`` 缺失（JSON 里没这个键）表示沿用 profile 的锁；显式写
    ``[]`` 表示“这条规则不加锁”。
    """

    match: str
    slot: str | None = None
    locks: list[str] | None = None
    timeout: int | None = None
    memory_max: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Rule":
        locks = data.get("locks")
        timeout = data.get("timeout")
        return cls(
            match=str(data.get("match") or ""),
            slot=(str(data["slot"]) if data.get("slot") else None),
            locks=None if locks is None else [str(x) for x in locks],
            timeout=parse_duration(timeout) if timeout is not None else None,
            memory_max=(str(data["memory_max"])
                        if data.get("memory_max") else None),
        )


def match_rule(profile: "Profile | None", cmd: list[str]) -> tuple[Rule | None, str]:
    """返回 (命中的规则, 用来匹配的命令字符串)。正则写错就跳过该条。"""
    command = shlex.join([str(x) for x in (cmd or [])])
    if profile is None:
        return None, command
    for rule in profile.rules:
        if not rule.match:
            continue
        try:
            if re.search(rule.match, command):
                return rule, command
        except re.error:
            continue
    return None, command


@dataclass
class Resolution:
    slot: str
    locks: list[str]
    timeout: int
    memory_max: str | None
    rule: Rule | None
    command: str
    profile: str = ""

    def describe(self) -> str:
        return (f"slot={self.slot} "
                f"locks={','.join(self.locks) or '-'} "
                f"timeout={self.timeout}s "
                f"rule={self.rule.match if self.rule else '(profile default)'}")


def resolve_job(profile: "Profile | None", cmd: list[str], *,
                slot: str | None = None, locks: list[str] | None = None,
                timeout: int | None = None, no_lock: bool = False,
                default_slot: str = "light",
                default_timeout: int = 3 * 3600) -> Resolution:
    """按「显式参数 > 规则 > profile 默认值」算出最终生效值。"""
    rule, command = match_rule(profile, cmd)
    if profile is None:
        base_slot, base_locks, base_timeout = default_slot, [], default_timeout
    else:
        base_slot = profile.slot
        base_locks = list(profile.locks or [])
        base_timeout = profile.timeout
    if rule is not None:
        if rule.slot:
            base_slot = rule.slot
        if rule.locks is not None:
            base_locks = list(rule.locks)
        if rule.timeout:
            base_timeout = rule.timeout
    if no_lock:
        base_locks = []
    merged: list[str] = []
    for lock in [*base_locks, *(locks or [])]:
        if lock not in merged:
            merged.append(lock)
    return Resolution(
        slot=str(slot or base_slot),
        locks=merged,
        timeout=int(timeout if timeout is not None else base_timeout),
        memory_max=(rule.memory_max if rule is not None else None),
        rule=rule,
        command=command,
        profile=(profile.name if profile is not None else ""),
    )


@dataclass
class Profile:
    name: str
    project: str
    host: str
    slot: str = "light"
    locks: list[str] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    timeout: int = 3 * 3600
    memory_max: str | None = None
    cpu_weight: str | None = None
    slice: str | None = None
    rules: list[Rule] = field(default_factory=list)
    unsupported: bool = False
    note: str = ""
    #: Cloud Run 执行环境（镜像、基础镜像、额外安装步骤），见 README「Cloud Run」。
    cloudrun: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.project + "@" + self.host

    @classmethod
    def from_dict(cls, data: dict, fallback_name: str) -> "Profile":
        name = data.get("name") or fallback_name
        project, _, host = name.partition("@")
        return cls(
            name=name,
            project=data.get("project") or project,
            host=data.get("host") or host,
            slot=str(data.get("slot") or "light"),
            locks=[str(x) for x in (data.get("locks") or [])],
            env_files=[str(x) for x in (data.get("env_files") or [])],
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            setup=[str(x) for x in (data.get("setup") or [])],
            artifacts=[str(x) for x in (data.get("artifacts") or [])],
            timeout=parse_duration(data.get("timeout"), 3 * 3600),
            memory_max=data.get("memory_max"),
            cpu_weight=(str(data["cpu_weight"]) if data.get("cpu_weight") is not None
                        else None),
            slice=(str(data["slice"]) if data.get("slice") else None),
            rules=[Rule.from_dict(item) for item in (data.get("rules") or [])],
            unsupported=bool(data.get("unsupported")),
            note=str(data.get("note") or ""),
            cloudrun=dict(data.get("cloudrun") or {}),
        )


def load_profiles(directory: str | Path) -> dict[str, Profile]:
    """加载目录顶层的 ``<project>@<host>.json``（子目录不会被读取）。

    ``profiles/examples/`` 只是可以复制到顶层的模板，不会自动生效。
    """
    directory = Path(directory)
    profiles: dict[str, Profile] = {}
    if not directory.is_dir():
        return profiles
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"invalid profile {path}: {exc}") from exc
        profile = Profile.from_dict(data, path.stem)
        profiles[profile.key] = profile
        profiles.setdefault(profile.name, profile)
    return profiles


def resolve_host(project: str, host: str | None = None,
                 platform: str | None = None,
                 profiles: dict[str, Profile] | None = None) -> str:
    """``ct run`` 没写 --host 时按 profile 推断目标主机。

    * 只有一个 profile 匹配该项目时用它；
    * ``--platform`` 时按主机名里是否含该平台关键字过滤（``runner-a-linux`` 这类）；
    * 其余情况要求显式 ``--host``，不做任何项目专属映射。

    ``project@*`` 适用于任意主机，给不出具体主机名，不参与推断；
    ``*@host`` / ``*@*`` 不属于某个项目，同样不算。
    """
    if host:
        return host
    candidates = []
    wildcard = ""
    seen = set()
    for profile in (profiles or {}).values():
        if profile.project != project or profile.unsupported:
            continue
        if profile.key in seen:
            continue
        seen.add(profile.key)
        if "*" in profile.host:
            wildcard = profile.key
            continue
        candidates.append(profile.host)
    hosts = list(dict.fromkeys(candidates))
    # 只剩 project@* 时报错里点名它，免得用户以为 profile 没被读到。
    hint = f"; {wildcard} matches any host" if wildcard else ""
    if platform:
        platform = platform.lower()
        matched = [name for name in hosts if platform in name.lower()]
        if len(matched) == 1:
            return matched[0]
        if not matched:
            raise ValueError(
                f"no host for project {project!r} on platform {platform!r}"
                f"{hint}; pass --host (profiles: {', '.join(hosts) or 'none'})")
        raise ValueError(
            f"several hosts for project {project!r} on platform {platform!r}: "
            f"{', '.join(matched)}; pass --host")
    if len(hosts) == 1:
        return hosts[0]
    if not hosts:
        if wildcard:
            raise ValueError(f"cannot infer a host for project {project!r}"
                             f"{hint}; pass --host")
        raise ValueError(f"cannot infer a host for project {project!r}; "
                         "pass --host or add a profile")
    raise ValueError(
        f"several hosts for project {project!r}: {', '.join(hosts)}; pass --host")


def find_profile(profiles: dict[str, Profile], project: str,
                 host: str) -> Profile | None:
    """按 ``project@host > project@* > *@host > *@*`` 取第一个存在的。"""
    for key in (f"{project}@{host}", f"{project}@*", f"*@{host}", f"*@*"):
        if key in profiles:
            return profiles[key]
    return None
