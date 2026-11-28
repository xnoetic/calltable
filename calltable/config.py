"""配置读取：单个 JSON 文件，hub 与 exec 共用。

样例见仓库根目录的 config.example.json。token 只放在真实配置文件里，
不进仓库；也可以用 CALLTABLE_TOKEN 环境变量覆盖。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HUB_URL = "http://127.0.0.1:8091"
#: 可选的 tailnet 名；留空表示不做 tailnet 归属检查（纯 HTTP/HTTPS + token 也能用）。
DEFAULT_OWNER_TAILNET = ""
DEFAULT_DATA_SUBDIR = ".calltable"
#: 执行器一次一任务、由 hub 按需拉起的 provider（见 ``calltable/cloudrun.py``）。
EPHEMERAL_PROVIDERS = ("cloudrun",)


class ConfigError(RuntimeError):
    pass


def _host_tiers(host: str, raw) -> dict[str, dict[str, str]]:
    """校验并规整 ``hosts.<name>.resource_defaults``（slot → {键: 值}）。

    与 hub 级 ``resource_defaults`` 一致：slot 名和键都不限定，值转成字符串；
    缺省 / null 表示不覆盖。结构不对直接报错，指明是哪台主机。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"host {host}: resource_defaults must be an object of "
                          "slot -> {memory_max, cpu_weight, slice}")
    tiers: dict[str, dict[str, str]] = {}
    for slot, spec in raw.items():
        if spec is None:
            continue
        if not isinstance(spec, dict):
            raise ConfigError(f"host {host}: resource_defaults.{slot} must be an "
                              "object such as {\"memory_max\": \"8G\"}")
        tiers[str(slot)] = {str(k): str(v) for k, v in spec.items()}
    return tiers


@dataclass
class HostSpec:
    name: str
    slots: dict[str, int] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    note: str = ""
    #: ``"static"``（默认，常驻执行器）或 ``"cloudrun"``（按需拉起的 Cloud Run
    #: job 执行）。见 ``calltable/cloudrun.py``。
    provider: str = "static"
    #: provider 专属配置（``provider == "cloudrun"`` 时是 Cloud Run 设置）。
    provider_config: dict = field(default_factory=dict)
    #: 这台主机自己的资源档位（``hosts.<name>.resource_defaults``），按 slot、
    #: 按键覆盖 hub 级 ``resource_defaults``。只含配置里写了的键。
    resource_defaults: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def ephemeral(self) -> bool:
        """执行器是否一次一任务、用完即走（心跳按 worker 对账）。

        只有已知的按需 provider 才算；未知的 provider 值（拼错等）hub 会告警，
        并完全按常驻主机处理。"""
        return self.provider in EPHEMERAL_PROVIDERS


#: ``machines.<name>.platform`` 的取值，与 ``ct run --platform`` 一致。
MACHINE_PLATFORMS = ("linux", "windows", "macos")
#: ``machines.<name>`` 允许的键（名字本身是对象的键）。
MACHINE_FIELDS = ("ssh", "platform", "purpose")
_MACHINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: ``machines.<name>.ssh``：一个 ssh 别名，或 ``user@host``；每段以字母、数字或
#: ``_`` 开头（所以不能以 ``-`` 开头被 ssh 当成选项），只含字母、数字和 ``._-``。
_MACHINE_SSH = re.compile(
    r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9_][A-Za-z0-9._-]*")


def parse_machines(raw) -> list[dict[str, str]]:
    """校验并规整客户端配置里的 ``machines``（本机登记的验证机）。

    形如 ``{"runner-a": {"ssh": "runner-a", "platform": "linux", "purpose": "..."}}``；
    缺省 / null 表示没有登记。calltable 排队不用它，只按契约
    （``spec/client-config.md``）校验格式并在 ``ct config show --json`` 里输出。
    返回按配置顺序排列的 ``[{name, ssh, platform, purpose}]``。
    """
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ConfigError("machines must be an object of name -> "
                          "{ssh, platform, purpose}")
    result: list[dict[str, str]] = []
    for name, spec in raw.items():
        name = str(name)
        if not _MACHINE_NAME.fullmatch(name):
            raise ConfigError(f"machines: invalid name {name!r} (letters, digits, "
                              "'.', '_', '-'; must start with a letter or digit)")
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            raise ConfigError(f"machines.{name} must be an object such as "
                              '{"ssh": "alias", "platform": "linux"}')
        unknown = sorted(set(map(str, spec)) - set(MACHINE_FIELDS))
        if unknown:
            raise ConfigError(f"machines.{name}: unknown field(s) "
                              f"{', '.join(unknown)}; allowed: "
                              f"{', '.join(MACHINE_FIELDS)}")
        entry = {"name": name}
        for key in MACHINE_FIELDS:
            value = spec.get(key)
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ConfigError(f"machines.{name}.{key} must be a string")
            entry[key] = value.strip()
        if entry["ssh"] and not _MACHINE_SSH.fullmatch(entry["ssh"]):
            raise ConfigError(f"machines.{name}.ssh must be a single ssh alias "
                              "or user@host (letters, digits, '.', '_', '-'; "
                              "not starting with '-')")
        entry["platform"] = entry["platform"].lower()
        if entry["platform"] and entry["platform"] not in MACHINE_PLATFORMS:
            raise ConfigError(f"machines.{name}.platform must be one of "
                              f"{', '.join(MACHINE_PLATFORMS)}")
        result.append(entry)
    return result


@dataclass
class Config:
    path: Path
    raw: dict
    token: str
    data_dir: Path
    #: 配置文件是怎么选中的：``flag`` / ``env`` / ``home`` / ``legacy``；
    #: 直接构造（测试、hub 进程内）时为空。
    source: str = ""
    #: 同时存在、但因为优先级更低而没有用上的用户级配置文件。
    shadowed: list[Path] = field(default_factory=list)

    @property
    def machines(self) -> list[dict[str, str]]:
        """本机登记的验证机（``machines``），见 :func:`parse_machines`。"""
        return parse_machines(self.raw.get("machines"))

    # ---- 通用
    @property
    def hub_url(self) -> str:
        return self.raw.get("hub_url", DEFAULT_HUB_URL).rstrip("/")

    @property
    def hub_proxy(self) -> str:
        """执行器访问 hub 时用的 HTTP 代理（``hub_proxy`` / ``CALLTABLE_HUB_PROXY``）。"""
        return (os.environ.get("CALLTABLE_HUB_PROXY", "").strip()
                or str(self.raw.get("hub_proxy") or "").strip())

    @property
    def owner_tailnet(self) -> str:
        return str(self.raw.get("owner_tailnet") or "")

    @property
    def beszel_url(self) -> str:
        return self.raw.get("beszel_url", "")

    @property
    def public_url(self) -> str:
        """对外可访问的看板地址（写进 webhook 载荷，不含 token）。"""
        return str(self.raw.get("public_url") or self.hub_url).rstrip("/")

    # ---- 0.3：预计时间 / 等待接口 / webhook / 幂等 / 心跳
    @property
    def estimate_window(self) -> int:
        return max(1, int(self.raw.get("estimate_window", 20)))

    @property
    def estimate_min_samples(self) -> int:
        return max(1, int(self.raw.get("estimate_min_samples", 3)))

    @property
    def max_wait_seconds(self) -> int:
        """长轮询的服务端上限（秒）。"""
        return max(1, min(600, int(self.raw.get("max_wait_seconds", 60))))

    @property
    def max_waiters(self) -> int:
        """同时挂起的长轮询连接上限。"""
        return max(1, int(self.raw.get("max_waiters", 16)))

    @property
    def max_event_streams(self) -> int:
        return max(1, int(self.raw.get("max_event_streams", 8)))

    @property
    def event_heartbeat_seconds(self) -> float:
        return max(0.1, float(self.raw.get("event_heartbeat_seconds", 15)))

    @property
    def event_retention(self) -> int:
        return max(100, int(self.raw.get("event_retention", 10000)))

    @property
    def webhook_secret(self) -> str:
        return str(self.raw.get("webhook_secret") or "").strip()

    @property
    def webhook_urls(self) -> list[str]:
        """hub 级别的全局叫号通知地址。"""
        configured = self.raw.get("webhook_urls") or []
        if isinstance(configured, str):
            configured = [configured]
        return [str(url) for url in configured if url]

    @property
    def webhook_timeout(self) -> float:
        return max(1.0, float(self.raw.get("webhook_timeout", 10)))

    @property
    def webhook_max_attempts(self) -> int:
        return max(1, int(self.raw.get("webhook_max_attempts", 6)))

    @property
    def webhook_retry_base(self) -> float:
        return max(0.05, float(self.raw.get("webhook_retry_base_seconds", 2)))

    @property
    def webhook_max_delay(self) -> float:
        return max(0.05, float(self.raw.get("webhook_max_delay_seconds", 300)))

    @property
    def webhook_retention_seconds(self) -> int:
        return max(60, int(self.raw.get("webhook_retention_seconds", 7 * 86400)))

    @property
    def idempotency_window_seconds(self) -> int:
        return max(1, int(self.raw.get("idempotency_window_seconds", 86400)))

    @property
    def janitor_interval(self) -> float:
        return max(0.05, float(self.raw.get("janitor_interval", 30)))

    @property
    def token_value(self) -> str:
        return os.environ.get("CALLTABLE_TOKEN", "").strip() or self.token

    # ---- hub
    @property
    def hub_bind(self) -> str:
        # 默认只监听回环；需要给内网用就在配置里显式写地址（A11）。
        return self.raw.get("hub_bind", "127.0.0.1")

    @property
    def hub_extra_binds(self) -> list[str]:
        """同一端口上额外监听的地址（例如给懒猫微服 app 反代用的虚拟机网卡）。

        与 ``hub_bind`` 相同的地址会被去掉；鉴权不变，所有 /api/* 仍要 token。
        """
        extra = self.raw.get("hub_extra_binds") or []
        if isinstance(extra, str):
            extra = [extra]
        seen = {self.hub_bind}
        result = []
        for addr in extra:
            addr = str(addr).strip()
            if addr and addr not in seen:
                seen.add(addr)
                result.append(addr)
        return result

    @property
    def hub_port(self) -> int:
        return int(self.raw.get("hub_port", 8091))

    @property
    def hosts(self) -> dict[str, HostSpec]:
        result = {}
        for name, spec in (self.raw.get("hosts") or {}).items():
            if isinstance(spec, int):
                result[name] = HostSpec(name=name, slots={"default": int(spec)})
                continue
            spec = spec or {}
            slots = spec.get("slots") or {}
            if isinstance(slots, int):
                slots = {"default": int(slots)}
            provider = str(spec.get("provider") or "static").strip().lower()
            result[name] = HostSpec(
                name=name,
                slots={str(k): int(v) for k, v in slots.items()},
                labels=list(spec.get("labels") or []),
                note=str(spec.get("note") or ""),
                provider=provider,
                provider_config=(dict(spec.get(provider) or {})
                                 if provider != "static" else {}),
                resource_defaults=_host_tiers(name, spec.get("resource_defaults")),
            )
        return result

    @property
    def default_slot(self) -> str:
        return str(self.raw.get("default_slot", "light"))

    @property
    def groups(self) -> dict[str, list[str]]:
        """主机组：``{"linux": ["runner-a", "runner-b"]}``，列表顺序即优先级。"""
        result = {}
        for name, members in (self.raw.get("groups") or {}).items():
            if isinstance(members, str):
                members = [members]
            result[str(name)] = [str(member) for member in members]
        return result

    @property
    def resource_defaults(self) -> dict[str, dict]:
        """按 slot 的资源档位默认值，可被 profile 覆盖。"""
        defaults = {
            "heavy": {"memory_max": "20G", "cpu_weight": "400",
                      "slice": "calltable-heavy.slice"},
            "light": {"memory_max": "4G", "cpu_weight": "200",
                      "slice": "calltable-light.slice"},
        }
        for slot, spec in (self.raw.get("resource_defaults") or {}).items():
            defaults.setdefault(str(slot), {}).update(
                {str(k): str(v) for k, v in (spec or {}).items()})
        return defaults

    def host_resource_tier(self, host: str, slot: str) -> dict[str, str]:
        """某台主机上某个 slot 的资源档位：主机级按键覆盖 hub 级。

        没配主机级档位（或主机不在配置里）就是 hub 级档位。
        """
        tier = dict(self.resource_defaults.get(slot, {}))
        spec = self.hosts.get(host)
        if spec is not None:
            tier.update(spec.resource_defaults.get(slot, {}))
        return tier

    @property
    def upstreams(self) -> dict[str, str]:
        """hub 主动拉取的只读上游：``{"example": "git@..."}``。"""
        return {str(k): str(v)
                for k, v in (self.raw.get("upstreams") or {}).items() if v}

    @property
    def upstream_interval(self) -> float:
        """上游 fetch 间隔（秒），默认 5 分钟。"""
        return max(30.0, float(self.raw.get("upstream_interval", 300)))

    @property
    def task_oom_score_adj(self) -> int:
        """任务进程的 oom_score_adj；OOM 时优先杀任务而不是执行器。"""
        return int(self.raw.get("task_oom_score_adj", 500))

    @property
    def cpu_rate_percent(self) -> int:
        """Windows Job Object 硬上限（百分比），默认 70。"""
        value = int(self.raw.get("cpu_rate_percent", 70))
        return max(1, min(100, value))

    def _config_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.path.parent / path).resolve()

    @property
    def profiles_dir(self) -> Path:
        """profile 目录：配置 → CALLTABLE_PROFILES_DIR → 配置旁边 → 安装源 → cwd。

        客户端配置通常只有 token/hub（ct setup 写的），不能因此丢掉 profile 里
        的 slot/lock/产物定义。
        """
        configured = self.raw.get("profiles_dir")
        candidates = []
        if configured:
            candidates.append(self._config_path(configured))
        env = os.environ.get("CALLTABLE_PROFILES_DIR")
        if env:
            candidates.append(Path(env))
        candidates.append(self.path.parent / "profiles")
        candidates.append(Path(__file__).resolve().parents[1] / "profiles")
        candidates.append(Path.cwd() / "profiles")
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return candidates[0] if candidates else self.path.parent / "profiles"

    # ---- exec
    @property
    def exec_host(self) -> str:
        value = self.raw.get("exec_host")
        if not value:
            raise ConfigError("exec_host is required in the executor config")
        return str(value)

    @property
    def runs_root(self) -> Path:
        value = self.raw.get("runs_root")
        return self._config_path(value) if value else self.data_dir / "runs"

    @property
    def mirror_root(self) -> Path:
        value = self.raw.get("mirror_root")
        return self._config_path(value) if value else self.data_dir / "mirrors"

    @property
    def max_jobs(self) -> int:
        return int(self.raw.get("max_jobs", 2))

    @property
    def poll_interval(self) -> float:
        return float(self.raw.get("poll_interval", 3))

    @property
    def sample_interval(self) -> float:
        return float(self.raw.get("sample_interval", 10))

    @property
    def retention_success(self) -> int:
        return int(self.raw.get("retention_success_seconds", 86400))

    @property
    def min_free_gb(self) -> float:
        """runs_root 所在分区的空闲下限（GB）；0 表示不检查。"""
        return float(self.raw.get("min_free_gb", 0) or 0)

    @property
    def runs_max_gb(self) -> float:
        """runs_root 目录总大小的上限（GB）；0 表示不检查。"""
        return float(self.raw.get("runs_max_gb", 0) or 0)

    @property
    def disk_scan_budget(self) -> float:
        """单次 runs 大小扫描的时间预算（秒），避免心跳被巨型目录拖死。"""
        return max(0.5, float(self.raw.get("disk_scan_budget_seconds", 8)))

    @property
    def retention_failure(self) -> int:
        return int(self.raw.get("retention_failure_seconds", 3 * 86400))

    @property
    def log_cap_bytes(self) -> int:
        return int(self.raw.get("log_cap_bytes", 256 * 1024 * 1024))

    @property
    def use_systemd(self) -> bool:
        configured = self.raw.get("use_systemd")
        if configured is None:
            return os.name != "nt" and bool(_which("systemd-run"))
        return bool(configured)

    @property
    def shell(self) -> str:
        """执行任务用的 shell。

        Windows 上必须给绝对路径：CreateProcess 的搜索顺序里 System32 优先于
        PATH，裸 ``bash`` 会命中 WSL 的占位程序（explain 里 "execvpe(/bin/bash)
        failed" 就是它），所以这里统一解析并优先选 Git Bash。
        """
        configured = self.raw.get("shell")
        if os.name == "nt":
            if configured:
                raw = str(configured)
                resolved = raw if os.path.isabs(raw) else (_which(raw) or "")
                if resolved and not _is_wsl_bash_stub(resolved):
                    return resolved
            for candidate in (
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files (x86)\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
            ):
                if os.path.exists(candidate):
                    return candidate
            found = _which("bash")
            if found and not _is_wsl_bash_stub(found):
                return found
            return "cmd.exe"
        return str(configured or "/bin/bash")


def _is_wsl_bash_stub(path: str) -> bool:
    """``bash`` 在 Windows 上常解析到 System32 的 WSL 占位程序，不能用。"""
    if os.name != "nt":
        return False
    lowered = path.lower().replace("/", "\\")
    return "\\system32\\" in lowered and lowered.endswith("bash.exe")


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


#: 配置文件的来源名（``ct config show --json`` 的 ``config.source``）。
SOURCE_FLAG = "flag"
SOURCE_ENV = "env"
SOURCE_HOME = "home"
SOURCE_LEGACY = "legacy"


def user_config_path() -> Path:
    """用户级配置的首选位置：``~/.calltable/config.json``（Windows 同规则）。

    ``ct setup`` 新装时写这里，与安装目录（``~/.calltable/src``）放在一起。
    """
    return Path.home() / DEFAULT_DATA_SUBDIR / "config.json"


def legacy_user_config_path() -> Path:
    """旧的用户级配置位置：``~/.config/calltable/config.json``，继续可用。"""
    return Path.home() / ".config" / "calltable" / "config.json"


def user_data_dir() -> Path:
    return Path.home() / DEFAULT_DATA_SUBDIR


@dataclass
class ConfigLocation:
    """:func:`locate_config` 的结果：用哪个文件、为什么选它、遮住了哪些。"""
    path: Path
    source: str
    shadowed: list[Path] = field(default_factory=list)


def user_config_candidates() -> list[tuple[str, Path]]:
    """用户级配置的候选位置，按优先级：新位置在前，旧位置在后。"""
    return [(SOURCE_HOME, user_config_path()),
            (SOURCE_LEGACY, legacy_user_config_path())]


def locate_config(explicit: str | None = None) -> ConfigLocation:
    """--config → CALLTABLE_CONFIG → ~/.calltable/config.json →
    ~/.config/calltable/config.json（顺序固定）。

    两个用户级文件都在时用新位置 ``~/.calltable/config.json``，旧文件记进
    ``shadowed``，由 ``ct config show`` / ``ct doctor`` 报告出来。
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"config not found: {path}")
        return ConfigLocation(path, SOURCE_FLAG)
    env = os.environ.get("CALLTABLE_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.exists():
            raise ConfigError(f"CALLTABLE_CONFIG points to a missing file: {path}")
        return ConfigLocation(path, SOURCE_ENV)
    # 用 is_file()：新位置上是个目录之类时不算，继续用旧文件，而不是选中它再
    # 处处报 "Is a directory"。空文件或坏文件仍是普通文件，照样选中并报错。
    found = [(source, path) for source, path in user_config_candidates()
             if path.is_file()]
    if found:
        source, path = found[0]
        return ConfigLocation(path, source, [other for _, other in found[1:]])
    raise ConfigError(
        f"no config file; run `ct setup --hub {DEFAULT_HUB_URL}` or pass --config")


def find_config(explicit: str | None = None) -> Path:
    """只要路径时用这个；查找顺序见 :func:`locate_config`。"""
    return locate_config(explicit).path


def load_config(explicit: str | None = None, *, require_token: bool = True) -> Config:
    location = locate_config(explicit)
    path = location.path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} must be a JSON object")
    token = (str(raw.get("token") or "").strip()
             or os.environ.get("CALLTABLE_TOKEN", "").strip())
    if not token and require_token:
        raise ConfigError(
            f"config {path} has no token; run `ct setup --hub {DEFAULT_HUB_URL}`")
    data_dir = Path(raw.get("data_dir") or (path.parent / "data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = (path.parent / data_dir).resolve()
    for name, spec in (raw.get("hosts") or {}).items():
        if isinstance(spec, dict):
            _host_tiers(str(name), spec.get("resource_defaults"))
    try:
        parse_machines(raw.get("machines"))
    except ConfigError as exc:
        raise ConfigError(f"config {path}: {exc}") from exc
    return Config(path=path, raw=raw, token=token, data_dir=data_dir,
                  source=location.source, shadowed=list(location.shadowed))
