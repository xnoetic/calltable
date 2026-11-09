"""0.3 叫号通知：Standard Webhooks 签名、投递、重试、持久化。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from calltable import webhooks
from calltable.db import Store
from calltable.hub import Hub
from calltable.webhooks import WebhookDispatcher, deliveries_for_display
from tests.helpers import make_config

RAW_SECRET = b"calltable-test-secret-bytes"
SECRET = "whsec_" + base64.b64encode(RAW_SECRET).decode()


class Receiver:
    """本地 HTTP 接收端：记录请求，按预设状态码回应。"""

    def __init__(self, status: int = 204):
        self.status = status
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({
                    "headers": {key.lower(): value
                                for key, value in self.headers.items()},
                    "body": body,
                })
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SigningTests(unittest.TestCase):
    def test_standard_webhooks_signature_is_hmac_sha256_over_id_ts_body(self):
        body = b'{"type":"job.finished"}'
        signature = webhooks.sign(SECRET, "msg_abc", 1762300000, body)
        expected = base64.b64encode(hmac.new(
            RAW_SECRET, b"msg_abc.1762300000." + body,
            hashlib.sha256).digest()).decode()
        self.assertEqual(signature, "v1," + expected)
        self.assertTrue(webhooks.verify(SECRET, "msg_abc", 1762300000, body,
                                        signature, tolerance=300,
                                        now_ts=1762300000))
        self.assertFalse(webhooks.verify(SECRET, "msg_abc", 1762300000, body,
                                         signature, tolerance=300,
                                         now_ts=1762301000))
        self.assertFalse(webhooks.verify(
            "whsec_" + base64.b64encode(b"other").decode(), "msg_abc",
            1762300000, body, signature, tolerance=300, now_ts=1762300000))

    def test_plain_secret_is_used_as_utf8_bytes(self):
        body = b"{}"
        signature = webhooks.sign("plain-secret", "msg_1", 10, body)
        expected = base64.b64encode(hmac.new(
            b"plain-secret", b"msg_1.10.{}", hashlib.sha256).digest()).decode()
        self.assertEqual(signature, "v1," + expected)

    def test_verify_accepts_multiple_v1_signatures(self):
        body = b"{}"
        good = webhooks.sign(SECRET, "msg_1", 10, body)
        header = "v1,AAAA " + good.split(",", 1)[1]
        self.assertTrue(webhooks.verify(SECRET, "msg_1", 10, body, header,
                                        tolerance=0, now_ts=10))

    def test_url_validation_and_log_redaction(self):
        self.assertEqual(webhooks.validate_url("https://example.com/x?y=1"),
                         "https://example.com/x?y=1")
        self.assertEqual(webhooks.validate_url("http://127.0.0.1:9/hook"),
                         "http://127.0.0.1:9/hook")
        for bad in ("ftp://example.com/x", "file:///etc/passwd", "",
                    "example.com/hook", "javascript:alert(1)"):
            with self.assertRaises(ValueError, msg=bad):
                webhooks.validate_url(bad)
        redacted = webhooks.redact_url(
            "https://hooks.slack.com/services/T000/B000/SECRETPATH?x=1")
        self.assertEqual(redacted, "https://hooks.slack.com/…")
        self.assertNotIn("SECRET", redacted)

    def test_build_headers_use_the_protocol_header_names(self):
        body = b"{}"
        headers = webhooks.build_headers(SECRET, "msg_1", body, timestamp=42,
                                         version="0.3.0")
        self.assertEqual(headers["webhook-id"], "msg_1")
        self.assertEqual(headers["webhook-timestamp"], "42")
        self.assertTrue(headers["webhook-signature"].startswith("v1,"))
        self.assertEqual(headers["User-Agent"], "calltable/0.3.0")
        self.assertEqual(headers["Content-Type"], "application/json")


class IntegrationExampleTests(unittest.TestCase):
    """integrations/ 里的示例必须能独立跑，且验签与 hub 一致。"""

    @staticmethod
    def _load(name: str, secret: str):
        root = Path(__file__).resolve().parents[1]
        path = root / "integrations" / name
        spec = importlib.util.spec_from_file_location(f"example_{name[:-3]}",
                                                      path)
        module = importlib.util.module_from_spec(spec)
        previous = os.environ.get("CALLTABLE_WEBHOOK_SECRET")
        os.environ["CALLTABLE_WEBHOOK_SECRET"] = secret
        try:
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                os.environ.pop("CALLTABLE_WEBHOOK_SECRET", None)
            else:
                os.environ["CALLTABLE_WEBHOOK_SECRET"] = previous
        return module

    def test_verify_example_accepts_hub_signatures(self):
        from calltable import webhooks as w
        module = self._load("verify_webhook.py", SECRET)
        body = b'{"type":"job.finished"}'
        timestamp = int(time.time())
        signature = w.sign(SECRET, "msg_test", timestamp, body)
        self.assertTrue(module.verify("msg_test", str(timestamp), signature,
                                      body))
        self.assertFalse(module.verify("msg_test", str(timestamp - 4000),
                                       signature, body))
        self.assertFalse(module.verify("msg_test", str(timestamp),
                                       signature, b"{}"))

    def test_forwarder_example_verifies_and_summarises(self):
        from calltable import webhooks as w
        module = self._load("forward_to_im.py", SECRET)
        body = json.dumps({
            "type": "job.finished", "duration_seconds": 12.5,
            "job": {"id": "job-1", "project": "demo", "host": "runner-a",
                    "slot": "light", "status": "succeeded", "exit_code": 0,
                    "label": "gate"},
        }).encode()
        timestamp = int(time.time())
        signature = w.sign(SECRET, "msg_fwd", timestamp, body)
        self.assertTrue(module.verify("msg_fwd", str(timestamp), signature,
                                      body))
        text = module.summary(json.loads(body))
        self.assertIn("job.finished", text)
        self.assertIn("demo@runner-a", text)
        self.assertIn("succeeded", text)


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.receiver = Receiver()
        self.db = self.root / "hub.sqlite3"
        self.store = Store(self.db)
        self.store.seed_host("h1", {"light": 1}, [])
        self.store.heartbeat("h1", "seed", [])

    def tearDown(self):
        self.receiver.close()
        self.store.close()
        self.tmp.cleanup()

    def _config(self, **overrides):
        raw = {
            "webhook_secret": SECRET,
            "webhook_timeout": 2,
            "webhook_max_attempts": 3,
            "webhook_retry_base_seconds": 0.01,
            "webhook_max_delay_seconds": 0.05,
            "webhook_urls": [],
        }
        raw.update(overrides)
        return make_config(self.root / "cfg", **raw)

    def _hub(self, **overrides):
        hub = Hub(self._config(**overrides))
        hub.store.seed_host("h1", {"light": 1}, [])
        hub.store.heartbeat("h1", "seed", [])
        return hub

    def _job(self, hub, **kw):
        defaults = dict(project="demo", host="h1", slot="light", locks=[],
                        cmd=["true"])
        defaults.update(kw)
        return hub.create_job(defaults)

    def test_started_and_finished_are_delivered_with_valid_signatures(self):
        hub = self._hub()
        try:
            job = self._job(hub, webhooks=[self.receiver.url])
            self.assertEqual(job["webhooks"], [self.receiver.url])
            dispatcher = WebhookDispatcher(hub.store, hub.config)
            hub.store.claim("h1", "w1")
            self.assertEqual(dispatcher.run_once(), 1)
            hub.store.finish(job["id"], status="succeeded", exit_code=0)
            self.assertEqual(dispatcher.run_once(), 1)

            self.assertEqual(len(self.receiver.requests), 2)
            events = []
            for request in self.receiver.requests:
                headers = request["headers"]
                body = request["body"]
                self.assertTrue(headers["webhook-id"].startswith("msg_"))
                self.assertTrue(headers["webhook-signature"].startswith("v1,"))
                self.assertTrue(webhooks.verify(
                    SECRET, headers["webhook-id"],
                    headers["webhook-timestamp"], body,
                    headers["webhook-signature"], tolerance=300))
                events.append(json.loads(body))
            self.assertEqual([event["type"] for event in events],
                             ["job.started", "job.finished"])
            self.assertEqual(events[1]["job"]["status"], "succeeded")
            self.assertEqual(events[1]["job"]["exit_code"], 0)
            self.assertIsInstance(events[1]["duration_seconds"], float)

            rows = hub.store.webhook_deliveries(job["id"])
            self.assertEqual({row["status"] for row in rows}, {"delivered"})
            self.assertEqual({row["attempts"] for row in rows}, {1})
            display = deliveries_for_display(rows)
            self.assertEqual(len(display), 1)
            self.assertEqual(len(display[0]["events"]), 2)
        finally:
            hub.close()
            hub.store.close()

    def test_global_hub_webhooks_are_added_to_job_webhooks(self):
        hub = self._hub(webhook_urls=[self.receiver.url])
        try:
            job = self._job(hub)
            hub.store.claim("h1", "w1")
            rows = hub.store.webhook_deliveries(job["id"])
            self.assertEqual([row["url"] for row in rows],
                             [self.receiver.url])
        finally:
            hub.close()
            hub.store.close()

    def test_failures_retry_then_give_up(self):
        self.receiver.status = 500
        hub = self._hub()
        try:
            job = self._job(hub, webhooks=[self.receiver.url])
            dispatcher = WebhookDispatcher(hub.store, hub.config)
            hub.store.claim("h1", "w1")
            for expected_attempt in (1, 2, 3):
                time.sleep(0.15)  # 等指数退避的 next_attempt 到期
                self.assertEqual(dispatcher.run_once(), 1)
                row = hub.store.webhook_deliveries(job["id"])[0]
                self.assertEqual(row["attempts"], expected_attempt)
                self.assertEqual(row["last_status"], 500)
            row = hub.store.webhook_deliveries(job["id"])[0]
            self.assertEqual(row["status"], "failed")
            self.assertIn("HTTP 500", row["last_error"])
            self.assertEqual(len(self.receiver.requests), 3)
            self.assertEqual(dispatcher.run_once(), 0)  # 不再重试
        finally:
            hub.close()
            hub.store.close()

    def test_pending_deliveries_survive_a_restart(self):
        hub = self._hub()
        try:
            job = self._job(hub, webhooks=[self.receiver.url])
            hub.store.claim("h1", "w1")
            hub.store.finish(job["id"], status="succeeded", exit_code=0)
        finally:
            hub.close()
            hub.store.close()

        reopened = Store(hub.store.path)
        try:
            self.assertEqual(len(reopened.due_webhook_deliveries()), 2)
            dispatcher = WebhookDispatcher(reopened, self._config())
            self.assertEqual(dispatcher.run_once(), 2)
            self.assertEqual({row["status"] for row in
                              reopened.webhook_deliveries(job["id"])},
                             {"delivered"})
        finally:
            reopened.close()

    def test_repeated_finish_does_not_enqueue_a_second_delivery(self):
        hub = self._hub()
        try:
            job = self._job(hub, webhooks=[self.receiver.url])
            hub.store.claim("h1", "w1")
            hub.store.finish(job["id"], status="succeeded", exit_code=0)
            hub.store.finish(job["id"], status="succeeded", exit_code=0)
            rows = hub.store.webhook_deliveries(job["id"])
            self.assertEqual(len(rows), 2)  # started + finished
        finally:
            hub.close()
            hub.store.close()

    def test_hub_rejects_webhooks_without_a_signing_secret(self):
        hub = Hub(make_config(self.root / "nocfg"))
        try:
            with self.assertRaises(ValueError) as ctx:
                hub.create_job({"project": "demo", "host": "h1",
                                "cmd": ["true"], "webhooks": [self.receiver.url]})
            self.assertIn("webhook", str(ctx.exception))
        finally:
            hub.close()
            hub.store.close()

    def test_hub_validates_webhook_urls_and_limits(self):
        hub = self._hub()
        try:
            with self.assertRaises(ValueError):
                hub.create_job({"project": "demo", "host": "h1",
                                "cmd": ["true"],
                                "webhooks": ["file:///etc/passwd"]})
            too_many = [f"https://example.com/{i}" for i in range(11)]
            with self.assertRaises(ValueError):
                hub.create_job({"project": "demo", "host": "h1",
                                "cmd": ["true"], "webhooks": too_many})
        finally:
            hub.close()
            hub.store.close()

    def test_job_detail_exposes_delivery_status(self):
        hub = self._hub()
        try:
            job = self._job(hub, webhooks=[self.receiver.url])
            hub.store.claim("h1", "w1")
            detail = hub.with_webhook_info(hub.store.job(job["id"]))
            self.assertEqual(detail["webhook_deliveries"][0]["url"],
                             self.receiver.url)
            self.assertEqual(
                detail["webhook_deliveries"][0]["events"][0]["status"],
                "pending")
        finally:
            hub.close()
            hub.store.close()


if __name__ == "__main__":
    unittest.main()
