"""脱敏的机器可读视图：``ct config show --json`` 与 ``ct doctor --json``。

其他工具（脚本、agent 技能）只调这两条命令，不直接解析带令牌的配置文件，
也不解析 ``MODE=...`` 那一行。输出结构是契约，见 ``spec/client-config.md``；
不兼容的改动必须加大 :data:`SCHEMA_VERSION`。

两份输出都**不含令牌**：只报告「有没有令牌、从哪来」。输出前还会把已知的
秘密值（令牌、hub 地址里的口令）从所有字符串里替换掉，作为兜底。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from . import __version__
from .config import (SOURCE_ENV, SOURCE_FLAG, Config, ConfigError, load_config,
                     locate_config, user_config_candidates)

#: 契约版本（``spec/client-config.md``）。只加字段不加版本；改名、删字段、
#: 改类型或改语义时加一。
SCHEMA_VERSION = 1
CONFIG_KIND = "calltable.config"
DOCTOR_KIND = "calltable.doctor"
TOKEN_ENV = "CALLTABLE_TOKEN"
REDACTED = "[redacted]"
#: 短于这个长度的值不做字符串替换（否则会把普通字符改坏）；真实令牌远长于此。
_MIN_SECRET_LEN = 8
#: :func:`doctor_payload` 替换自由文本时临时代替脱敏后 hub 地址的占位符。
_URL_PLACEHOLDER = "\x00hub-url\x00"


def redact_url(url: str | None) -> str | None:
    """去掉 URL 里的 ``user:password@``、查询串和片段（它们可能带凭据）。"""
    if not url:
        return url
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return REDACTED
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def url_secrets(url: str | None) -> list[str]:
    """URL 里可能是凭据的部分：用户名、口令、查询串的值（原样和解码后各一份）。"""
    if not url:
        return []
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return []
    found = [parts.username or "", parts.password or ""]
    if parts.query:
        found.extend(value for item in parts.query.split("&")
                     for value in item.split("=", 1)[1:])
    found += [unquote(value) for value in found]
    return [value for value in found if value]


def scrub(value, secrets, *, min_len: int = _MIN_SECRET_LEN) -> object:
    """递归地把字符串里出现的秘密值替换成 :data:`REDACTED`。

    默认只替换不短于 :data:`_MIN_SECRET_LEN` 的值；``min_len=1`` 用于只含
    自由文本的字段（见 :func:`doctor_payload`）。"""
    secrets = sorted({str(s).strip() for s in secrets
                      if s and len(str(s).strip()) >= max(min_len, 1)},
                     key=len, reverse=True)
    if not secrets:
        return value

    def walk(item):
        if isinstance(item, str):
            for secret in secrets:
                if secret in item:
                    item = item.replace(secret, REDACTED)
            return item
        if isinstance(item, dict):
            return {walk(k): walk(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(v) for v in item]
        return item

    return walk(value)


def _env_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def token_source(config: Config | None, override: str | None = None) -> str:
    """``flag`` / ``env`` / ``config`` / ``none``，与实际生效的优先级一致。"""
    if override and override.strip():
        return "flag"
    if _env_token():
        return "env"
    if config is not None and str(config.raw.get("token") or "").strip():
        return "config"
    return "none"


def search_order(explicit: str | None = None) -> list[dict]:
    """配置查找顺序上的每一处：来源、路径、文件在不在。"""
    entries: list[dict] = []
    if explicit:
        path = Path(explicit).expanduser()
        entries.append({"source": SOURCE_FLAG, "path": str(path),
                        "exists": path.exists()})
    env = os.environ.get("CALLTABLE_CONFIG")
    if env:
        path = Path(env).expanduser()
        entries.append({"source": SOURCE_ENV, "path": str(path),
                        "exists": path.exists()})
    for source, path in user_config_candidates():
        entries.append({"source": source, "path": str(path.resolve()),
                        "exists": path.is_file()})
    return entries


def _location(explicit: str | None) -> tuple[str | None, str | None, list[str]]:
    try:
        location = locate_config(explicit)
    except ConfigError:
        return None, None, []
    return (str(location.path.resolve()), location.source,
            [str(p.resolve()) for p in location.shadowed])


def config_block(config: Config | None, explicit: str | None,
                 error: str | None) -> dict:
    """两份输出共用的 ``config`` 对象：用的是哪个文件、为什么、遮住了哪些。"""
    if config is not None:
        path, source = str(config.path), config.source or None
        shadowed = [str(p.resolve()) for p in config.shadowed]
    else:
        path, source, shadowed = _location(explicit)
    return {"path": path, "source": source, "shadowed": shadowed,
            "search_order": search_order(explicit), "error": error or None}


def config_show(explicit: str | None = None) -> tuple[int, dict]:
    """``ct config show --json``：(退出码, 输出对象)。

    退出码 0 表示找到并读通了配置；2 表示没有配置或配置有错（对象照样输出，
    ``config.error`` 里是原因）。
    """
    error = None
    config: Config | None = None
    try:
        config = load_config(explicit, require_token=False)
    except ConfigError as exc:
        error = str(exc)
    secrets = [_env_token()]
    if config is not None:
        secrets += [config.token, str(config.raw.get("token") or "")]
        secrets += url_secrets(config.hub_url)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": CONFIG_KIND,
        "client_version": __version__,
        "config": config_block(config, explicit, error),
        "hub_url": redact_url(config.hub_url) if config is not None else None,
        "token_present": bool(config.token_value if config is not None
                              else _env_token()),
        "token_source": token_source(config),
        "owner_tailnet": config.owner_tailnet if config is not None else "",
        "machines": config.machines if config is not None else [],
    }
    return (0 if config is not None else 2), scrub(payload, secrets)


def doctor_payload(result: dict, *, config: Config | None, explicit: str | None,
                   config_error: str | None, token: str,
                   token_override: str | None = None) -> dict:
    """``ct doctor --json``：把 :func:`calltable.doctor.report` 的结果按契约输出。"""
    hub = dict(result["hub"])
    raw_url = hub["url"]
    shown_url = redact_url(raw_url)
    hidden = url_secrets(raw_url)
    secrets = [token, token_override or "", _env_token(), *hidden]
    if config is not None:
        secrets.append(str(config.raw.get("token") or ""))
    hub["url"] = shown_url

    def free_text(text: str) -> str:
        # 原因和明细里写的是原始 hub 地址：先整体换成脱敏后的地址（期间用占位符
        # 护住它），再把 URL 里的用户名、口令、查询值不论长短都替换掉。
        if raw_url and shown_url != raw_url:
            text = text.replace(raw_url, _URL_PLACEHOLDER)
        text = scrub(text, hidden, min_len=1)
        return text.replace(_URL_PLACEHOLDER, shown_url or "")

    hub["detail"] = free_text(hub["detail"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": DOCTOR_KIND,
        "client_version": __version__,
        "mode": result["mode"],
        "ready": result["code"] == 0,
        "exit_code": result["code"],
        "line": free_text(result["line"]),
        "reasons": [free_text(reason) for reason in result["reasons"]],
        "config": config_block(config, explicit, config_error),
        "token_present": bool(token),
        "token_source": token_source(config, token_override),
        "hub": hub,
        "checks": {"python": result["python"], "git": result["git"],
                   "tailscale": result["tailscale"]},
        "hosts": result["hosts"],
        "hosts_error": free_text(result["hosts_error"]) or None,
        "groups": result["groups"],
    }
    return scrub(payload, secrets)


def format_config_text(payload: dict) -> list[str]:
    """``ct config show``（不带 --json）给人看的几行。"""
    block = payload["config"]
    lines = []
    if block["path"]:
        lines.append(f"config  {block['path']} (source={block['source']})")
    else:
        lines.append("config  (none)")
    for path in block["shadowed"]:
        lines.append(f"ignored {path} (lower priority; "
                     "~/.calltable/config.json wins)")
    if block["error"]:
        lines.append(f"error   {block['error']}")
    lines.append(f"hub     {payload['hub_url'] or '(none)'}")
    lines.append(f"token   {'present' if payload['token_present'] else 'missing'}"
                 f" (source={payload['token_source']})")
    if payload["machines"]:
        lines.append("machines:")
        for machine in payload["machines"]:
            bits = [f"ssh={machine['ssh'] or '-'}",
                    f"platform={machine['platform'] or '-'}"]
            if machine["purpose"]:
                bits.append(machine["purpose"])
            lines.append(f"  {machine['name']}: " + "  ".join(bits))
    return lines


__all__ = ["SCHEMA_VERSION", "CONFIG_KIND", "DOCTOR_KIND", "REDACTED",
           "redact_url", "scrub", "config_show", "doctor_payload",
           "format_config_text", "search_order", "token_source"]
