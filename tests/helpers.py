"""测试辅助：构造 Config / 等待条件。"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from calltable.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 命令前缀：在非特权 user + PID namespace 里跑，后面的命令就是 PID 1，而且
#: 没有 init（和 Cloud Run 容器一样）。``--mount-proc`` 让 /proc 与新 namespace
#: 对得上。
UNSHARE_PID1 = ["unshare", "--user", "--map-root-user", "--pid", "--fork",
                "--mount-proc"]


@functools.lru_cache(maxsize=None)
def can_unshare_pid1() -> bool:
    """这台主机允许非特权 user + PID namespace 吗（容器里通常不允许）。"""
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return False
    try:
        probe = subprocess.run([*UNSHARE_PID1, "true"], capture_output=True,
                               timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def bash_path() -> str:
    if os.name != "nt":
        return "/bin/bash"
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash") or "bash"
    if "system32" in found.lower():
        raise RuntimeError("only the WSL bash stub is on PATH; install Git Bash")
    return found


def make_config(root: Path, *, hosts: dict | None = None,
                profiles: bool = True, **overrides) -> Config:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if profiles:
        (root / "profiles").mkdir(exist_ok=True)
    raw = {
        "token": "test-token",
        "data_dir": str(root / "data"),
        "profiles_dir": str(root / "profiles"),
        "hub_url": "http://127.0.0.1:1",
        "hub_bind": "127.0.0.1",
        "hub_port": 0,
        "hosts": hosts if hosts is not None else {
            "h1": {"slots": {"heavy": 1, "light": 2}},
            "h2": {"slots": {"heavy": 1, "light": 1}},
        },
        "exec_host": "h1",
        "runs_root": str(root / "runs"),
        "mirror_root": str(root / "mirrors"),
        "max_jobs": 2,
        "poll_interval": 0.05,
        "sample_interval": 0.2,
        "use_systemd": False,
        "shell": bash_path(),
    }
    raw.update(overrides)
    path = root / "config.json"
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    data_dir = Path(raw["data_dir"])
    return Config(path=path, raw=raw, token=raw["token"], data_dir=data_dir)


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met before timeout")
