#!/usr/bin/env python3
"""Forward verified calltable webhooks to Slack or Feishu/Lark incoming hooks.

Standard library only. This is the "leave your phone number" flow taken one
step further: the hub calls this relay with a signed event, the relay verifies
it and posts a human-readable message to a chat room.

    export CALLTABLE_WEBHOOK_SECRET=whsec_...
    # Slack: create an incoming webhook, then
    export CALLTABLE_IM_KIND=slack
    export CALLTABLE_IM_URL=https://hooks.slack.com/services/...
    # or Feishu/Lark: group bot -> incoming webhook, then
    export CALLTABLE_IM_KIND=feishu
    export CALLTABLE_IM_URL=https://open.feishu.cn/open-apis/bot/v2/hook/...
    python integrations/forward_to_im.py

    ct run --detach --notify http://127.0.0.1:9099/hook --project demo -- <cmd>

Optional: CALLTABLE_IM_TOKEN for Feishu custom bots that validate a token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("CALLTABLE_WEBHOOK_SECRET", "")
KIND = os.environ.get("CALLTABLE_IM_KIND", "slack").lower()
IM_URL = os.environ.get("CALLTABLE_IM_URL", "")
IM_TOKEN = os.environ.get("CALLTABLE_IM_TOKEN", "")
PORT = int(os.environ.get("CALLTABLE_WEBHOOK_PORT", "9099"))
TOLERANCE = int(os.environ.get("CALLTABLE_WEBHOOK_TOLERANCE", "300"))

EMOJI = {
    "job.started": "🟦",
    "job.finished": "✅",
}
STATUS_EMOJI = {"succeeded": "✅", "failed": "❌", "cancelled": "⚪",
                "timeout": "⏰", "error": "🛠️"}


def signing_key(secret: str) -> bytes:
    text = secret.strip()
    if text.startswith("whsec_"):
        text = text[len("whsec_"):]
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            return text.encode("utf-8")
    return text.encode("utf-8")


def verify(msg_id: str, timestamp: str, signature: str, body: bytes) -> bool:
    if not SECRET:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > TOLERANCE:
        return False
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    expected = base64.b64encode(
        hmac.new(signing_key(SECRET), signed, hashlib.sha256).digest()
    ).decode("ascii")
    return any(hmac.compare_digest(part, expected)
               for part in signature.replace(",", " ").split())


def summary(event: dict) -> str:
    job = event.get("job") or {}
    kind = str(event.get("type") or "")
    icon = EMOJI.get(kind) or STATUS_EMOJI.get(job.get("status"), "🔔")
    duration = event.get("duration_seconds")
    parts = [
        f"{icon} calltable {kind}",
        f"{job.get('project')}@{job.get('host')} · {job.get('slot')}",
        f"status={job.get('status')} exit={job.get('exit_code')}",
    ]
    if duration is not None:
        parts.append(f"duration={duration:.1f}s")
    if job.get("label"):
        parts.append(f"label={job['label']}")
    if job.get("id"):
        parts.append(str(job["id"]))
    if event.get("dashboard_url"):
        parts.append(str(event["dashboard_url"]))
    return " | ".join(parts)


def post_to_im(text: str) -> None:
    if not IM_URL:
        print("CALLTABLE_IM_URL is not set; printing instead", file=sys.stderr)
        print(text)
        return
    if KIND == "feishu":
        payload = {"msg_type": "text", "content": {"text": text}}
        if IM_TOKEN:
            payload["content"]["token"] = IM_TOKEN
    else:  # slack (and most Mattermost-compatible hooks)
        payload = {"text": text}
    request = urllib.request.Request(
        IM_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read(1024)
    except Exception as exc:  # a chat outage must not produce a webhook retry storm
        print(f"forward to {KIND} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        msg_id = self.headers.get("webhook-id", "")
        if not verify(msg_id, self.headers.get("webhook-timestamp", ""),
                      self.headers.get("webhook-signature", ""), body):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            event = json.loads(body.decode("utf-8"))
        except ValueError:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        post_to_im(summary(event))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> int:
    if not SECRET:
        print("CALLTABLE_WEBHOOK_SECRET is required", file=sys.stderr)
        return 2
    if KIND not in ("slack", "feishu"):
        print("CALLTABLE_IM_KIND must be slack or feishu", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"calltable -> {KIND} relay on http://127.0.0.1:{PORT}/hook")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
