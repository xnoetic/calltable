"""通用小工具：时间、ID、超时解析、子进程辅助。"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import uuid

# ---------------------------------------------------------------- 时间与 ID

def now() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def new_id(prefix: str = "job") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- 超时解析

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value, default: int | None = None) -> int:
    """把 '3h' / '90m' / '45' / 3600 解析成秒。"""
    if value is None:
        if default is None:
            raise ValueError("duration is required")
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text:
        if default is None:
            raise ValueError("duration is empty")
        return default
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text)
    if not match:
        raise ValueError(f"invalid duration: {value!r}")
    number, unit = match.groups()
    return int(float(number) * _UNITS[unit or "s"])


_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3,
               "t": 1024 ** 4}


def parse_size(value) -> int | None:
    """把 '16G' / '512M' / 1024 解析成字节；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace("ib", "").replace("b", "")
    if not text:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)", text)
    if not match:
        return None
    number, unit = match.groups()
    return int(float(number) * _SIZE_UNITS[unit])


def version_tuple(text) -> tuple[int, ...]:
    """'0.2.0' / 'calltable/0.2.0' → (0, 2, 0)；无法解析返回 ()。"""
    if not text:
        return ()
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text))
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def human_bytes(n) -> str:
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ---------------------------------------------------------------- 提交人

def default_submitter() -> str:
    """默认提交人：CALLTABLE_SUBMITTER，否则 hostname/username。"""
    explicit = os.environ.get("CALLTABLE_SUBMITTER", "").strip()
    if explicit:
        return explicit
    try:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    except Exception:  # pragma: no cover - 极端环境
        user = "unknown"
    try:
        host = socket.gethostname()
    except Exception:  # pragma: no cover
        host = "host"
    return f"{host}/{user}"


#: 项目名统一格式（I4）：字母数字开头，可含 . _ -，最长 64。
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_project_name(name: str) -> bool:
    return bool(name) and bool(PROJECT_RE.fullmatch(name))


#: 随提交送给 hub 的产物 glob（``artifact_patterns``）的上限。仓库声明由写被测
#: 代码的人提供，glob 转成的正则会回溯，所以限制条数、长度和通配段数；
#: hub（``Hub._job_artifact_patterns``）和客户端（``repoconf``）共用这一份。
MAX_ARTIFACT_PATTERNS = 100
MAX_ARTIFACT_PATTERN_LENGTH = 256
#: 连续的 ``*`` 算一段（``**`` 是一段，``*a*`` 是两段）。
MAX_ARTIFACT_PATTERN_WILDCARDS = 4


def artifact_pattern_problem(pattern: str) -> str | None:
    """检查一条（已去掉首尾空白的）产物 glob；合格返回 None，否则返回原因。"""
    # 与平台无关地判断：执行器可能在 Windows 上，hub 在 Linux 上。
    if (".." in re.split(r"[\\/]+", pattern)
            or pattern.startswith(("/", "\\"))
            or re.match(r"[A-Za-z]:", pattern)):
        return f"must be a worktree-relative glob: {pattern!r}"
    if len(pattern) > MAX_ARTIFACT_PATTERN_LENGTH:
        return (f"must be at most {MAX_ARTIFACT_PATTERN_LENGTH} characters, "
                f"got {len(pattern)}")
    runs = len(re.findall(r"\*+", pattern))
    if runs > MAX_ARTIFACT_PATTERN_WILDCARDS:
        return (f"must contain at most {MAX_ARTIFACT_PATTERN_WILDCARDS} "
                f"wildcard runs ('*' or '**'), got {runs}: {pattern!r}")
    return None


# ---------------------------------------------------------------- 小工具

def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value, default=None):
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def is_windows() -> bool:
    return sys.platform.startswith("win")


def tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]
