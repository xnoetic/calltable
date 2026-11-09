#!/usr/bin/env python3
"""Minimal calltable webhook receiver with Standard Webhooks verification.

Standard library only. Run it, point `ct run --notify` at it, and watch the
events:

    CALLTABLE_WEBHOOK_SECRET=whsec_... python integrations/verify_webhook.py
    ct run --detach --notify http://127.0.0.1:9099/hook --demo -- <cmd>

The script verifies `webhook-id` / `webhook-timestamp` / `webhook-signature`
(HMAC-SHA256 over "{id}.{timestamp}.{body}"), rejects timestamps outside the
tolerance, de-duplicates on `webhook-id`, and prints the job summary.

Do not use it as-is in production: it has no TLS and no persistence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("CALLTABLE_WEBHOOK_SECRET", "")
TOLERANCE = int(os.environ.get("CALLTABLE_WEBHOOK_TOLERANCE", "300"))
PORT = int(os.environ.get("CALLTABLE_WEBHOOK_PORT", "9099"))
_seen: set[str] = set()


def signing_key(secret: str) -> bytes:
    text = secret.strip()
    if text.startswith("whsec_"):
        text = text[len("whsec_"):]
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            return text.encode("utf-8")
    return text.encode("utf-8")


def expected_signature(secret: str, msg_id: str, timestamp: str,
                       body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(signing_key(secret), signed, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify(msg_id: str, timestamp: str, signature: str, body: bytes) -> bool:
    if not SECRET:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > TOLERANCE:
        return False
    expected = expected_signature(SECRET, msg_id, timestamp, body)
    for candidate in signature.replace(",", " ").split():
        if hmac.compare_digest(candidate, expected):
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the demo output readable
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        msg_id = self.headers.get("webhook-id", "")
        timestamp = self.headers.get("webhook-timestamp", "")
        signature = self.headers.get("webhook-signature", "")

        if not verify(msg_id, timestamp, signature, body):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(f"rejected unsigned/invalid delivery id={msg_id!r}",
                  file=sys.stderr)
            return

        if msg_id in _seen:
            # Retries reuse webhook-id; receivers must be idempotent.
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(f"duplicate {msg_id} (already handled)")
            return
        _seen.add(msg_id)

        try:
            event = json.loads(body.decode("utf-8"))
        except ValueError:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        job = event.get("job") or {}
        print(f"{event.get('type')}  {job.get('id')}  "
              f"{job.get('project')}@{job.get('host')}  "
              f"status={job.get('status')} exit={job.get('exit_code')}  "
              f"duration={event.get('duration_seconds')}s  "
              f"{event.get('dashboard_url') or ''}")
        sys.stdout.flush()

        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> int:
    if not SECRET:
        print("CALLTABLE_WEBHOOK_SECRET is required (the hub's webhook_secret "
              "value)", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"calltable webhook receiver on http://127.0.0.1:{PORT}/hook")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
