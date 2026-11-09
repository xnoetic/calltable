"""配置读取：单个 JSON 文件，hub 与 exec 共用。

样例见仓库根目录的 config.example.json。token 只放在真实配置文件里，
不进仓库；也可以用 CALLTABLE_TOKEN 环境变量覆盖。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HUB_URL = "http://127.0.0.1:8091"
#: 可选的 tailnet 名；留空表示不做 tailnet 归属检查（纯 HTTP/HTTPS + token 也能用）。
DEFAULT_OWNER_TAILNET = ""
DEFAULT_DATA_SUBDIR = ".calltable"


class ConfigError(RuntimeError):
    pass


@dataclass
class HostSpec:
    name: str
    slots: dict[str, int] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Config:
    path: Path
    raw: dict
    token: str
    data_dir: Path

    # ---- 通用
    @property
    def hub_url(self) -> str:
        return self.raw.get("hub_url", DEFAULT_HUB_URL).rstrip("/")

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
            result[name] = HostSpec(
                name=name,
                slots={str(k): int(v) for k, v in slots.items()},
                labels=list(spec.get("labels") or []),
                note=str(spec.get("note") or ""),
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

    @property
    def profiles_dir(self) -> Path:
        """profile 目录：配置 → CALLTABLE_PROFILES_DIR → 配置旁边 → 安装源 → cwd。

        客户端配置通常只有 token/hub（ct setup 写的），不能因此丢掉 profile 里
        的 slot/lock/产物定义。
        """
        configured = self.raw.get("profiles_dir")
        candidates = []
        if configured:
            candidates.append(Path(configured))
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
        return Path(self.raw.get("runs_root") or (self.data_dir / "runs"))

    @property
    def mirror_root(self) -> Path:
        return Path(self.raw.get("mirror_root") or (self.data_dir / "mirrors"))

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


def user_config_path() -> Path:
    """用户级配置：~/.config/calltable/config.json（Windows 同路径规则）。"""
    return Path.home() / ".config" / "calltable" / "config.json"


def user_data_dir() -> Path:
    return Path.home() / DEFAULT_DATA_SUBDIR


def find_config(explicit: str | None = None) -> Path:
    """--config → CALLTABLE_CONFIG → ~/.config/calltable/config.json（顺序固定）。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"config not found: {path}")
        return path
    env = os.environ.get("CALLTABLE_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.exists():
            raise ConfigError(f"CALLTABLE_CONFIG points to a missing file: {path}")
        return path
    path = user_config_path()
    if path.exists():
        return path
    raise ConfigError(
        f"no config file; run `ct setup --hub {DEFAULT_HUB_URL}` or pass --config")


def load_config(explicit: str | None = None, *, require_token: bool = True) -> Config:
    path = find_config(explicit).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} must be a JSON object")
    token = str(raw.get("token") or "").strip()
    if not token and require_token:
        raise ConfigError(
            f"config {path} has no token; run `ct setup --hub {DEFAULT_HUB_URL}`")
    data_dir = Path(raw.get("data_dir") or (path.parent / "data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = (path.parent / data_dir).resolve()
    return Config(path=path, raw=raw, token=token, data_dir=data_dir)
