"""``ct doctor``：一眼判断这台机器能不能用 calltable。

输出固定为一行（stdout 只有这一行，明细走 stderr）：

* ``MODE=tailscale READY``                  退出码 0
* ``MODE=tailscale NOT-READY: <原因>``        退出码 3
* ``MODE=direct READY``                     退出码 0
* ``MODE=direct NOT-READY: <原因>``           退出码 3

tailscale 只是**可选**的组网方式：装了、并且配置了 owner_tailnet 且当前
tailnet 匹配时按 tailscale 模式检查；否则按 direct 模式检查，普通
HTTP/HTTPS + token 照样可用。两种模式都会检查 python/git/hub/auth。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .client import HubClient, HubError
from .config import DEFAULT_OWNER_TAILNET
from .util import version_tuple

MIN_PYTHON = (3, 12)
MIN_GIT = (2, 20)


def _ascii(text: str) -> str:
    """MODE 行给 agent 解析，不把系统本地化错误（中文/乱码）带进去。"""
    return str(text).encode("ascii", "replace").decode("ascii")

WINDOWS_TAILSCALE = Path(r"C:\Program Files\Tailscale\tailscale.exe")
WSL_TAILSCALE = Path("/mnt/c/Program Files/Tailscale/tailscale.exe")
MACOS_TAILSCALE = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def is_wsl() -> bool:
    """WSL 里没有 Linux tailscale CLI，但可以通过 interop 用 Windows 的。"""
    try:
        if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
            return True
        return "microsoft" in Path("/proc/version").read_text(
            encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def tailscale_candidates() -> list:
    candidates: list = []
    found = shutil.which("tailscale")
    if found:
        candidates.append(found)
    if is_wsl():
        candidates.append(WSL_TAILSCALE)
        exe = shutil.which("tailscale.exe")
        if exe:
            candidates.append(exe)
        candidates.append("tailscale.exe")
    candidates.append(WINDOWS_TAILSCALE)
    candidates.append(MACOS_TAILSCALE)
    return candidates


def find_tailscale() -> str | None:
    """PATH → WSL interop → Windows 默认路径 → macOS App bundle。"""
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in tailscale_candidates():
        if not isinstance(candidate, str) and hasattr(candidate, "exists"):
            if candidate.exists():
                return candidate
            continue
        if candidate == found:
            continue
        if candidate == "tailscale.exe" or Path(candidate).exists():
            return candidate
    return None


def parse_tailscale_status(text: str) -> dict:
    """从 ``tailscale status --json`` 里取 tailnet 名与 Self 在线状态。"""
    try:
        data = json.loads(text)
    except ValueError:
        return {"ok": False, "tailnet": "", "online": False,
                "error": "invalid JSON from tailscale"}
    if not isinstance(data, dict):
        return {"ok": False, "tailnet": "", "online": False,
                "error": "unexpected tailscale output"}
    tailnet = ""
    current = data.get("CurrentTailnet")
    if isinstance(current, dict):
        tailnet = str(current.get("Name") or current.get("MagicDNSName") or "")
    self_info = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    online = bool(self_info.get("Online", False))
    return {"ok": True, "tailnet": tailnet, "online": online, "error": ""}


def tailscale_status(exe: str) -> dict:
    try:
        proc = subprocess.run([exe, "status", "--json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "tailnet": "", "online": False,
                "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"ok": False, "tailnet": "", "online": False,
                "error": detail[-1] if detail else f"rc={proc.returncode}"}
    return parse_tailscale_status(proc.stdout)


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def git_version() -> tuple[int, ...]:
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ()
    return _version_tuple(proc.stdout) if proc.returncode == 0 else ()


def hub_health(hub_url: str, timeout: float = 5.0) -> tuple[bool, str, str]:
    """返回 (health_ok, hub version, 说明)。"""
    try:
        with urllib.request.urlopen(hub_url.rstrip("/") + "/api/health",
                                    timeout=timeout) as resp:
            if resp.status != 200:
                return False, "", f"health HTTP {resp.status}"
            try:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            return True, str(payload.get("version") or ""), ""
    except urllib.error.HTTPError as exc:
        return False, "", f"health HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, "", f"health unreachable: {getattr(exc, 'reason', exc)}"


def check_hub(hub_url: str, token: str, timeout: float = 5.0) -> tuple[bool, bool, str]:
    """返回 (health_ok, auth_ok, 说明)。"""
    ok, _version, detail = hub_health(hub_url, timeout=timeout)
    if not ok:
        return False, False, detail
    if not token:
        return True, False, "no token configured"
    try:
        HubClient(hub_url, token, timeout=timeout).get_json("/api/hosts")
    except HubError as exc:
        return True, False, f"auth failed: {exc}"
    return True, True, ""


def evaluate(*, owner_tailnet: str = "", tailscale_installed: bool = False,
             tailnet: str = "", online: bool = False,
             python_ok: bool, git_ok: bool, git_version_text: str,
             hub_ok: bool, auth_ok: bool, hub_url: str,
             detail: str = "", client_version: str = __version__,
             hub_version: str = "") -> tuple[int, str]:
    """纯函数：根据探测结果给出 (退出码, 固定格式那一行)。

    ``owner_tailnet`` 为空或 tailscale 不可用时走 direct 模式；tailscale
    不是硬依赖，缺省安装不影响使用。
    """
    tailscale_mode = bool(
        tailscale_installed and owner_tailnet
        and tailnet.strip().lower() == owner_tailnet.strip().lower())
    mode = "tailscale" if tailscale_mode else "direct"

    reasons: list[str] = []
    if tailscale_mode and not online:
        reasons.append("tailscale offline; fix: `tailscale up`")
    if not python_ok:
        reasons.append(f"python {sys.version.split()[0]} < "
                       f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}; fix: install Python "
                       f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
    if not git_ok:
        got = git_version_text or "not found"
        reasons.append(f"git {got} < {MIN_GIT[0]}.{MIN_GIT[1]}; fix: install git")
    if not hub_ok:
        reasons.append(f"hub {hub_url} unreachable ({_ascii(detail) or 'no response'}); "
                       f"fix: `curl {hub_url}/api/health`")
    elif not auth_ok:
        reasons.append(f"hub auth failed ({_ascii(detail) or 'no token'}); "
                       f"fix: `ct setup --hub {hub_url}`")
    if (hub_ok and hub_version
            and version_tuple(client_version) < version_tuple(hub_version)):
        reasons.append(f"client {client_version} older than hub {hub_version}; "
                       f"run ct self-update")
    if reasons:
        return 3, f"MODE={mode} NOT-READY: " + "; ".join(reasons)
    return 0, f"MODE={mode} READY"


def host_disk_details(hub_url: str, token: str, timeout: float = 5.0) -> list[str]:
    """hub 上各执行主机的磁盘/内存/排队信息（``ct doctor`` 明细用）。"""
    if not token:
        return []
    try:
        state = HubClient(hub_url, token, timeout=timeout).get_json("/api/state")
    except HubError as exc:
        return [f"hosts: unavailable ({_ascii(exc)})"]
    lines: list[str] = []
    for host in state.get("hosts") or []:
        system = host.get("system") or {}
        bits = ["online" if host.get("online") else "offline"]
        if system:
            if system.get("disk_free_gb") is not None:
                bits.append(f"disk_free={system['disk_free_gb']}G")
            if system.get("disk_runs_gb") is not None:
                bits.append(f"runs={system['disk_runs_gb']}G")
            if system.get("mem_free_gb"):
                bits.append(f"mem={system['mem_free_gb']}G")
            if system.get("load1"):
                bits.append(f"load={system['load1']}")
        bits.append(f"queue={host.get('queue', 0)}")
        if host.get("disk_low"):
            bits.append("WARNING disk_low: "
                        + str(system.get("disk_reason") or "runs over watermark"))
        lines.append(f"host {host.get('name')}: " + " ".join(bits))
    return lines


def run(*, hub_url: str, token: str, owner_tailnet: str = DEFAULT_OWNER_TAILNET,
        json_config: dict | None = None) -> tuple[int, str, list[str]]:
    """执行全部探测，返回 (退出码, 输出行, 明细行)。"""
    details: list[str] = []
    exe = find_tailscale()
    installed = exe is not None
    tailnet, online = "", False
    if exe:
        status = tailscale_status(exe)
        installed = status["ok"]
        tailnet = status["tailnet"]
        online = status["online"]
        details.append(f"tailscale: {exe}")
        details.append(f"tailnet: {tailnet or '(unknown)'} "
                       f"online={online} {status['error']}".rstrip())
    else:
        details.append("tailscale: not found")

    python_ok = sys.version_info >= MIN_PYTHON
    sv = git_version()
    git_ok = bool(sv) and sv[:2] >= MIN_GIT
    details.append(f"python: {sys.version.split()[0]} ok={python_ok}")
    details.append(f"git: {'.'.join(map(str, sv)) or 'not found'} ok={git_ok}")

    hub_ok, auth_ok, hub_detail = check_hub(hub_url, token)
    _health_ok, hub_version, _ = hub_health(hub_url)
    details.append(f"hub: {hub_url} health={hub_ok} auth={auth_ok} "
                   f"version={hub_version or '?'} {hub_detail}".rstrip())
    details.append(f"client: {__version__}")
    if hub_ok and auth_ok:
        details.extend(host_disk_details(hub_url, token))

    code, line = evaluate(
        owner_tailnet=owner_tailnet, tailscale_installed=installed,
        tailnet=tailnet, online=online, python_ok=python_ok, git_ok=git_ok,
        git_version_text=".".join(map(str, sv)), hub_ok=hub_ok,
        auth_ok=auth_ok, hub_url=hub_url, detail=hub_detail,
        client_version=__version__, hub_version=hub_version)
    return code, line, details
