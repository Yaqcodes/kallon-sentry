#!/usr/bin/env python3
"""Kallon hub alert listener — HMAC-verifying webhook receiver.

Runs on the customer hub (one per customer org). Receives signed alert POSTs
from towers over WireGuard, verifies the `X-Kallon-Signature` HMAC against the
shared alert key, and forwards/logs verified events. This is the hub-side end of
the integration contract in docs/alert-webhook.md.

Stdlib only (no pip deps) so it runs on a minimal hub image.

Env:
  ALERT_KEY_PATH   path to the base64 HMAC key (default /etc/kallon/alert.key)
  ALERT_BIND       bind address (default 10.50.0.1 — the wg0 gateway IP)
  ALERT_PORT       port (default 8080)
  ALERT_FORWARD_URL  optional: POST verified alerts here (e.g. control plane /v1/alerts/ingest)
  ALERT_INGEST_TOKEN optional: sent as X-Kallon-Ingest-Token on forward (matches KALLON_ALERT_INGEST_TOKEN)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alert-listener")

ALERT_KEY_PATH = os.environ.get("ALERT_KEY_PATH", "/etc/kallon/alert.key")
ALERT_BIND = os.environ.get("ALERT_BIND", "10.50.0.1")
ALERT_PORT = int(os.environ.get("ALERT_PORT", "8080"))
ALERT_FORWARD_URL = os.environ.get("ALERT_FORWARD_URL", "")


def _load_key() -> bytes:
    with open(ALERT_KEY_PATH, "rb") as fh:
        return fh.read().strip()


def verify(body: bytes, signature: str, key: bytes) -> bool:
    """Constant-time compare of sha256=<hex> against HMAC-SHA256(body)."""
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    provided = signature.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, provided)


class Handler(BaseHTTPRequestHandler):
    server_version = "kallon-hub/1.0"

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/alerts":
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        sig = self.headers.get("X-Kallon-Signature", "")

        try:
            key = _load_key()
        except OSError as e:
            log.error("alert key unreadable: %s", e)
            self._reply(500, {"error": "server key error"})
            return

        if not verify(body, sig, key):
            log.warning("REJECT bad signature from %s", self.client_address[0])
            self._reply(401, {"error": "invalid signature"})
            return

        try:
            alert = json.loads(body)
        except json.JSONDecodeError:
            self._reply(400, {"error": "invalid json"})
            return

        log.info("ALERT ok device=%s type=%s", alert.get("device_id"), alert.get("type"))
        if ALERT_FORWARD_URL:
            self._forward(body)
        self._reply(200, {"status": "accepted"})

    @staticmethod
    def _forward(body: bytes) -> None:
        headers = {"Content-Type": "application/json"}
        ingest_token = os.environ.get("ALERT_INGEST_TOKEN", "")
        if ingest_token:
            headers["X-Kallon-Ingest-Token"] = ingest_token
        try:
            req = urllib.request.Request(
                ALERT_FORWARD_URL, data=body,
                headers=headers, method="POST",
            )
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        except Exception as e:  # noqa: BLE001
            log.error("forward failed: %s", e)

    def log_message(self, *args) -> None:  # silence default access logging
        pass


def main() -> None:
    httpd = ThreadingHTTPServer((ALERT_BIND, ALERT_PORT), Handler)
    log.info("listening on %s:%d (key=%s, forward=%s)",
             ALERT_BIND, ALERT_PORT, ALERT_KEY_PATH, ALERT_FORWARD_URL or "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
