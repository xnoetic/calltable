"""``ct setup``：一条命令在新机器上配好客户端配置。

token 获取顺序：``--token`` → ``--token-file <path>`` → 环境变量
``CALLTABLE_TOKEN`` → 报错并提示。没有任何隐式的远端读取：token 必须由用户
显式提供（文件本机读取），这样安装脚本不依赖 ssh 别名或固定路径。

写到哪里：``--config`` 给了就写那里；否则新装写 ``~/.calltable/config.json``，
已经只有旧位置 ``~/.config/calltable/config.json`` 的机器就地更新旧文件（不
悄悄多出一份新文件把它遮住）。见 :func:`default_target`。
文件权限：Unix 0600（目录 0700），Windows 用 icacls 把继承权限去掉、只留当前用户。
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from pathlib import Path

from .config import (DEFAULT_HUB_URL, DEFAULT_OWNER_TAILNET, ConfigError,
                     legacy_user_config_path, user_config_path)

TOKEN_ENV = "CALLTABLE_TOKEN"


class SetupError(RuntimeError):
    pass


def resolve_token(explicit: str | None = None,
                  environ: dict | None = None,
                  runner=None,
                  token_file: str | None = None) -> str:
    """按 --token → --token-file → CALLTABLE_TOKEN 的顺序取 token。

    ``runner`` 仅为兼容旧的调用签名保留，不再执行任何外部命令。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    if token_file:
        path = Path(token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SetupError(
                f"cannot read --token-file {path}: {exc}; pass --token or set "
                f"{TOKEN_ENV}") from exc
        if not token:
            raise SetupError(f"--token-file {path} is empty")
        return token.splitlines()[0].strip()
    env_token = ((environ or os.environ).get(TOKEN_ENV) or "").strip()
    if env_token:
        return env_token
    raise SetupError(
        f"no token given; pass --token, --token-file <path>, or set {TOKEN_ENV}")


def _restrict_posix(path: Path) -> None:
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)


def _restrict_windows(path: Path) -> None:
    user = os.environ.get("USERNAME") or getpass.getuser()
    # 去掉继承 ACE，只给当前用户完全控制；失败不致命（文件本身已经写好）。
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"{user}:F"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def write_config(path: str | Path, data: dict) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    if os.name == "nt":
        _restrict_windows(path)
    else:
        _restrict_posix(path)
    return path


def merge_config(path: Path, updates: dict) -> dict:
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data.update(updates)
    return data


def default_target() -> Path:
    """``ct setup`` 不带 ``--config`` 时写的文件。

    新位置已存在、或两处都没有（新装）→ ``~/.calltable/config.json``；
    只有旧位置 → 就地更新 ``~/.config/calltable/config.json``，行为与以前一致。
    """
    preferred = user_config_path()
    legacy = legacy_user_config_path()
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


def setup(*, hub: str | None = None, token: str | None = None,
          owner_tailnet: str | None = None,
          path: str | Path | None = None,
          environ: dict | None = None,
          runner=None,
          token_file: str | None = None) -> tuple[Path, dict]:
    hub_url = (hub or DEFAULT_HUB_URL).rstrip("/")
    resolved = resolve_token(token, environ=environ, runner=runner,
                             token_file=token_file)
    if not resolved:
        raise SetupError("empty token")
    target = Path(path).expanduser() if path else default_target()
    updates = {"hub_url": hub_url, "token": resolved}
    if owner_tailnet or DEFAULT_OWNER_TAILNET:
        updates["owner_tailnet"] = owner_tailnet or DEFAULT_OWNER_TAILNET
    data = merge_config(target, updates)
    write_config(target, data)
    return target, data


def repo_root() -> Path:
    """当前安装所在的 git 仓库根（``ct self-update`` 用）。"""
    return Path(__file__).resolve().parents[1]


def self_update(root: str | Path | None = None,
                runner=subprocess.run) -> tuple[int, str]:
    root = Path(root) if root else repo_root()
    if not (root / ".git").exists():
        raise SetupError(f"{root} is not a git checkout; reinstall with bin/install.sh")
    try:
        proc = runner(["git", "-C", str(root), "pull", "--ff-only"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"git pull failed: {exc}") from exc
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


__all__ = ["SetupError", "resolve_token", "write_config", "merge_config",
           "default_target", "setup", "self_update", "repo_root", "ConfigError", "TOKEN_ENV"]
