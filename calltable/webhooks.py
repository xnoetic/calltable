"""Webhook 签名与后台投递（Standard Webhooks，仅标准库）。

签名格式见 https://www.standardwebhooks.com/ ：

* ``webhook-id``       每条消息稳定的 id（重试不变，接收端据此去重）；
* ``webhook-timestamp`` 本次尝试的 Unix 秒；
* ``webhook-signature`` ``v1,base64(HMAC-SHA256(key, "{id}.{ts}.{body}"))``。

密钥来自 hub 配置 ``webhook_secret``：``whsec_`` 前缀按规范 base64 解码，
否则按 UTF-8 字节使用。投递记录全部落库（``webhook_deliveries``），
hub 重启后继续投递；失败按指数退避重试，有次数上限。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import urllib.error
import urllib.parse
import urllib.request

from .util import now

#: 接收端建议的时钟容差（秒）；本模块只用于示例/测试的校验辅助。
SIGNATURE_TOLERANCE = 300


def signing_key(secret: str) -> bytes:
    text = (secret or "").strip()
    if text.startswith("whsec_"):
        text = text[len("whsec_"):]
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            return text.encode("utf-8")
    return text.encode("utf-8")


def sign(secret: str, msg_id: str, timestamp: int, payload: bytes) -> str:
    """生成 ``webhook-signature`` 头的值（含 ``v1,`` 前缀）。"""
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + payload
    digest = hmac.new(signing_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify(secret: str, msg_id: str, timestamp, payload: bytes, signature: str,
           tolerance: int = SIGNATURE_TOLERANCE,
           now_ts: float | None = None) -> bool:
    """接收端校验：时间戳容差 + 常量时间比较（支持空格分隔的多个签名）。"""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now() if now_ts is None else now_ts
    if tolerance >= 0 and abs(current - ts) > tolerance:
        return False
    expected = sign(secret, msg_id, ts, payload)
    for candidate in str(signature or "").replace(",", " ").split():
        if hmac.compare_digest(candidate, expected.split(",", 1)[1]):
            return True
    return False


def validate_url(url: str) -> str:
    """只接受 http/https；返回规范化后的 URL，失败抛 ValueError。"""
    text = str(url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"webhook URL must be http or https: {text!r}")
    if not parsed.netloc:
        raise ValueError(f"webhook URL has no host: {text!r}")
    return text


def redact_url(url: str) -> str:
    """日志用：保留 scheme 与 host，丢掉路径/query（Slack 的密钥在路径里）。"""
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.netloc:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.netloc}/…"


def build_headers(secret: str, msg_id: str, payload: bytes,
                  timestamp: int | None = None, version: str = "") -> dict:
    ts = int(now() if timestamp is None else timestamp)
    headers = {
        "Content-Type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign(secret, msg_id, ts, payload),
    }
    if version:
        headers["User-Agent"] = f"calltable/{version}"
    return headers


class WebhookDispatcher(threading.Thread):
    """单个后台线程按顺序投递；不阻塞调度路径。"""

    def __init__(self, store, config, stop_event: threading.Event | None = None):
        super().__init__(daemon=True, name="hub-webhooks")
        self.store = store
        self.secret = config.webhook_secret
        self.timeout = config.webhook_timeout
        self.max_attempts = config.webhook_max_attempts
        self.retry_base = config.webhook_retry_base
        self.max_delay = config.webhook_max_delay
        from . import __version__
        self.version = __version__
        self._stop = stop_event or threading.Event()
        self._idle_wait = 0.25

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - 常驻循环
        while not self._stop.is_set():
            try:
                handled = self.run_once()
            except Exception as exc:
                print(f"[hub] webhook dispatcher: {type(exc).__name__}: {exc}",
                      flush=True)
                handled = 0
            if not handled:
                self._stop.wait(self._idle_wait)

    def run_once(self) -> int:
        """投递一批到期记录；返回本轮处理的条数（测试可直接调用）。"""
        deliveries = self.store.due_webhook_deliveries(now(), limit=20)
        for delivery in deliveries:
            self.deliver(delivery)
        return len(deliveries)

    def deliver(self, delivery: dict) -> bool:
        attempts = self.store.webhook_attempt(delivery["id"])
        payload = str(delivery["payload"]).encode("utf-8")
        ok, status_code, error = self._post(delivery["url"], delivery["msg_id"],
                                            payload)
        if ok:
            self.store.webhook_delivered(delivery["id"], status_code)
            return True
        if attempts >= self.max_attempts:
            self.store.webhook_failed(delivery["id"], status_code, error)
            print(f"[hub] webhook {delivery['event']} to "
                  f"{redact_url(delivery['url'])} failed after {attempts} "
                  f"attempt(s): {error}", flush=True)
            return False
        delay = min(self.retry_base * (2 ** max(0, attempts - 1)),
                    self.max_delay)
        self.store.webhook_retry_later(delivery["id"], now() + delay,
                                       status_code, error)
        print(f"[hub] webhook {delivery['event']} to "
              f"{redact_url(delivery['url'])} failed (attempt {attempts}, "
              f"retry in {delay:.1f}s): {error}", flush=True)
        return False

    def _post(self, url: str, msg_id: str, payload: bytes
              ) -> tuple[bool, int | None, str]:
        if not self.secret:
            return False, None, "hub has no webhook_secret configured"
        headers = build_headers(self.secret, msg_id, payload,
                                version=self.version)
        request = urllib.request.Request(url, data=payload, method="POST",
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                resp.read(1024)
                status = int(getattr(resp, "status", 200))
                if 200 <= status < 300:
                    return True, status, ""
                return False, status, f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            try:
                exc.read(1024)
            except OSError:
                pass
            message = f"HTTP {exc.code}"
            if exc.code >= 500 or exc.code == 429:
                return False, exc.code, message
            return False, exc.code, message
        except (urllib.error.URLError, OSError, ValueError) as exc:
            detail = getattr(exc, "reason", exc)
            return False, None, f"{type(exc).__name__}: {detail}"


def deliveries_for_display(rows: list[dict]) -> list[dict]:
    """把投递记录按 URL 聚合成 ``ct show`` / API 需要的形状。"""
    grouped: dict[str, dict] = {}
    for row in rows:
        entry = grouped.setdefault(str(row["url"]), {"url": row["url"],
                                                    "events": []})
        entry["events"].append({
            "event": row["event"],
            "status": row["status"],
            "attempts": row["attempts"],
            "last_status": row["last_status"],
            "last_error": row["last_error"],
            "msg_id": row["msg_id"],
            "updated": row["updated"],
        })
    return list(grouped.values())
