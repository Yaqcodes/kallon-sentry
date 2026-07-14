#!/usr/bin/env python3
"""Kallon hub tower HTTP proxy — Artemis → hub → tower gateway over wg0.

Runs on the customer hub. The control plane (Artemis) never joins the customer
VPN; it calls this agent on the hub's public IP with a shared token. The agent
forwards to ``http://{tower-vpn-ip}:8766/...`` over WireGuard.

Stdlib only (no pip deps) so it runs on a minimal hub image.

Env:
  HUB_PROXY_BIND          bind address (default 0.0.0.0 — public)
  HUB_PROXY_PORT          port (default 8767)
  HUB_PROXY_TOKEN         required shared secret (X-Kallon-Hub-Proxy-Token)
  TOWER_GATEWAY_PORT      tower gateway port (default 8766)
  HUB_PROXY_CONNECT_SEC   connect timeout to tower (default 3)
  HUB_PROXY_READ_SEC      read timeout (default 30; covers snapshots)
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tower-proxy")

HUB_PROXY_BIND = os.environ.get("HUB_PROXY_BIND", "0.0.0.0")
HUB_PROXY_PORT = int(os.environ.get("HUB_PROXY_PORT", "8767"))
HUB_PROXY_TOKEN = os.environ.get("HUB_PROXY_TOKEN", "")
TOWER_GATEWAY_PORT = int(os.environ.get("TOWER_GATEWAY_PORT", "8766"))
PROXY_TIMEOUT_SEC = float(os.environ.get("HUB_PROXY_READ_SEC", "30"))

_TOKEN_HEADER = "X-Kallon-Hub-Proxy-Token"
_VPN_IP_HEADER = "X-Kallon-Tower-Vpn-Ip"
# /proxy/{device_id}/api/...  or  /proxy/{device_id}/healthz
_PROXY_RE = re.compile(r"^/proxy/([^/]+)(/.*)$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _err_body(code: str, message: str, **ctx) -> bytes:
    return json.dumps({"error": {"code": code, "message": message, **ctx}}).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "kallon-hub-proxy/1.0"
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        if not HUB_PROXY_TOKEN:
            log.error("HUB_PROXY_TOKEN unset — rejecting all proxy requests")
            return False
        provided = self.headers.get(_TOKEN_HEADER, "")
        return provided == HUB_PROXY_TOKEN

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/healthz":
            self._reply(200, b'{"status":"ok"}')
            return
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def _proxy(self) -> None:
        if not self._auth_ok():
            self._reply(401, _err_body("unauthorized", "missing or invalid hub proxy token"))
            return

        path_only = self.path.split("?", 1)[0]
        m = _PROXY_RE.match(path_only)
        if not m:
            self._reply(404, _err_body("not_found", f"expected /proxy/{{device_id}}/..., got {path_only}"))
            return

        device_id, rest = m.group(1), m.group(2)
        if not rest.startswith("/api/") and rest != "/healthz":
            self._reply(404, _err_body("not_found", f"refusing to proxy path {rest!r}"))
            return

        vpn_ip = (self.headers.get(_VPN_IP_HEADER) or "").strip()
        if not vpn_ip or not _IPV4_RE.match(vpn_ip):
            self._reply(
                422,
                _err_body(
                    "invalid_request",
                    f"missing or invalid {_VPN_IP_HEADER}",
                    device_id=device_id,
                ),
            )
            return

        qs = ""
        if "?" in self.path:
            qs = "?" + self.path.split("?", 1)[1]
        target = f"http://{vpn_ip}:{TOWER_GATEWAY_PORT}{rest}{qs}"

        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length > 0 else None

        headers = {}
        ctype = self.headers.get("Content-Type")
        if ctype:
            headers["Content-Type"] = ctype
        if self.headers.get("Accept"):
            headers["Accept"] = self.headers["Accept"]

        try:
            req = urllib.request.Request(target, data=body, headers=headers, method=self.command)
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_SEC) as resp:  # noqa: S310
                payload = resp.read()
                ct = resp.headers.get("Content-Type", "application/octet-stream")
                self._reply(resp.status, payload, content_type=ct)
        except urllib.error.HTTPError as exc:
            payload = exc.read() if exc.fp else b""
            ct = exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json"
            self._reply(exc.code, payload or _err_body("tower_error", str(exc.reason), device_id=device_id), ct)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("tower %s unreachable at %s: %s", device_id, target, exc)
            self._reply(
                503,
                _err_body(
                    "tower_offline",
                    f"tower did not respond ({type(exc).__name__}) — VPN tunnel down or tower rebooting",
                    device_id=device_id,
                ),
            )

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    if not HUB_PROXY_TOKEN:
        log.warning("HUB_PROXY_TOKEN is empty — all /proxy/* requests will return 401")
    httpd = ThreadingHTTPServer((HUB_PROXY_BIND, HUB_PROXY_PORT), Handler)
    log.info(
        "listening on %s:%d (tower_port=%d)",
        HUB_PROXY_BIND, HUB_PROXY_PORT, TOWER_GATEWAY_PORT,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
