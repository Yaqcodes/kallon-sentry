#!/usr/bin/env python3
"""Kallon hub HLS proxy — Artemis → hub MediaMTX ← tower RTSP over wg0.

Artemis never joins the customer VPN. It dials this agent on the hub public IP
with the shared hub token; the agent ensures a MediaMTX path that pulls
``rtsp://{tower-vpn-ip}:8554/camN`` (on-demand) and streams HLS from local
MediaMTX (loopback :8888) back to Artemis.

Stdlib only.

Env:
  HUB_HLS_BIND            bind address (default 0.0.0.0)
  HUB_HLS_PORT            port (default 8768)
  HUB_PROXY_TOKEN         required shared secret (X-Kallon-Hub-Proxy-Token)
  MEDIAMTX_API            default http://127.0.0.1:9997
  MEDIAMTX_HLS            default http://127.0.0.1:8888
  TOWER_RTSP_PORT         default 8554
  HUB_HLS_IDLE_CLOSE      MediaMTX sourceOnDemandCloseAfter (default 30s)
  HUB_HLS_READ_SEC        forward read timeout (default 60)
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hls-proxy")

HUB_HLS_BIND = os.environ.get("HUB_HLS_BIND", "0.0.0.0")
HUB_HLS_PORT = int(os.environ.get("HUB_HLS_PORT", "8768"))
HUB_PROXY_TOKEN = os.environ.get("HUB_PROXY_TOKEN", "")
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997").rstrip("/")
MEDIAMTX_HLS = os.environ.get("MEDIAMTX_HLS", "http://127.0.0.1:8888").rstrip("/")
TOWER_RTSP_PORT = int(os.environ.get("TOWER_RTSP_PORT", "8554"))
# Buyer HLS pulls the tower's low-bitrate path (camN_sub). Main camN stays for
# NVR / NOC. Override with HUB_HLS_TOWER_PATH_SUFFIX="" only for labs without subs.
TOWER_HLS_PATH_SUFFIX = os.environ.get("HUB_HLS_TOWER_PATH_SUFFIX", "_sub")
IDLE_CLOSE = os.environ.get("HUB_HLS_IDLE_CLOSE", "30s")
PROXY_TIMEOUT_SEC = float(os.environ.get("HUB_HLS_READ_SEC", "60"))

_TOKEN_HEADER = "X-Kallon-Hub-Proxy-Token"
_VPN_IP_HEADER = "X-Kallon-Tower-Vpn-Ip"
# /hls/{device_id}/cam{n}/...   n = 1..16
_HLS_RE = re.compile(r"^/hls/([^/]+)/(cam(?:[1-9]|1[0-6]))(/.*)?$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_DEVICE_RE = re.compile(r"^kln_[a-z0-9]+_\d{6}$")


def _err_body(code: str, message: str, **ctx) -> bytes:
    return json.dumps({"error": {"code": code, "message": message, **ctx}}).encode()


def _mtx_path_name(device_id: str, cam: str) -> str:
    # MediaMTX path names: alphanumeric + underscore. device_id already matches.
    return f"{device_id}_{cam}"


def _http_json(method: str, url: str, body: Optional[dict] = None, timeout: float = 5.0) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


def _tower_rtsp_path(cam: str) -> str:
    """Tower MediaMTX path to pull for buyer HLS (default camN_sub)."""
    suffix = TOWER_HLS_PATH_SUFFIX.strip()
    return f"{cam}{suffix}" if suffix else cam


def ensure_path(device_id: str, cam: str, vpn_ip: str) -> Optional[str]:
    """Ensure MediaMTX has an on-demand path pulling tower RTSP. Return error message or None.

    If the hub path already exists, leave it alone. MediaMTX reloads config on
    every API write; PATCHing per playlist/segment destroys the HLS muxer and
    looks like RTP loss / DTS errors under dashboard load.
    """
    name = _mtx_path_name(device_id, cam)
    tower_path = _tower_rtsp_path(cam)
    source = f"rtsp://{vpn_ip}:{TOWER_RTSP_PORT}/{tower_path}"
    conf = {
        "name": name,
        "source": source,
        "sourceOnDemand": True,
        "sourceOnDemandStartTimeout": "15s",
        "sourceOnDemandCloseAfter": IDLE_CLOSE,
        "sourceProtocol": "tcp",
    }
    get_code, get_body = _http_json(
        "GET", f"{MEDIAMTX_API}/v3/config/paths/get/{quote(name, safe='')}"
    )
    if get_code == 200:
        # Path exists — do not PATCH. (VPN IP changes are rare; delete the path
        # or restart mediamtx after hub reprovision if the tower IP moves.)
        try:
            cur = json.loads(get_body.decode() or "{}")
            cur_src = cur.get("source") if isinstance(cur, dict) else None
        except (json.JSONDecodeError, AttributeError, TypeError):
            cur_src = None
        if isinstance(cur_src, str) and cur_src and cur_src != source:
            log.warning(
                "hub path %s source drift (have %s, want %s) — not auto-patching; "
                "delete path or restart kallon-hub-mediamtx after cutover",
                name, cur_src, source,
            )
        return None

    add_code, add_body = _http_json(
        "POST",
        f"{MEDIAMTX_API}/v3/config/paths/add/{quote(name, safe='')}",
        conf,
    )
    if add_code in (200, 201):
        log.info("added MediaMTX path %s → %s", name, source)
        return None
    # Race: path created between GET and POST
    if add_code in (400, 409):
        return None
    return f"mediamtx path add failed ({add_code}): {add_body[:200]!r}"


class Handler(BaseHTTPRequestHandler):
    server_version = "kallon-hub-hls/1.0"
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        if not HUB_PROXY_TOKEN:
            log.error("HUB_PROXY_TOKEN unset — rejecting all HLS requests")
            return False
        return self.headers.get(_TOKEN_HEADER, "") == HUB_PROXY_TOKEN

    def do_GET(self) -> None:  # noqa: N802
        path_only = self.path.split("?", 1)[0]
        if path_only == "/healthz":
            # Soft-check local MediaMTX API.
            code, _ = _http_json("GET", f"{MEDIAMTX_API}/v3/config/global/get", timeout=2.0)
            if code == 200:
                self._reply(200, b'{"status":"ok","mediamtx":true}')
            else:
                self._reply(503, _err_body("mediamtx_down", f"MediaMTX API returned {code}"))
            return
        self._serve_hls()

    def do_HEAD(self) -> None:  # noqa: N802
        # Rare; treat like GET without special-casing body.
        self._serve_hls()

    def _serve_hls(self) -> None:
        if not self._auth_ok():
            self._reply(401, _err_body("unauthorized", "missing or invalid hub proxy token"))
            return

        path_only = self.path.split("?", 1)[0]
        m = _HLS_RE.match(path_only)
        if not m:
            self._reply(
                404,
                _err_body("not_found", f"expected /hls/{{device_id}}/camN/..., got {path_only}"),
            )
            return

        device_id, cam, rest = m.group(1), m.group(2), m.group(3) or "/index.m3u8"
        if not _DEVICE_RE.match(device_id):
            self._reply(422, _err_body("invalid_request", f"bad device_id {device_id!r}"))
            return
        if not rest.startswith("/"):
            rest = "/" + rest

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

        err = ensure_path(device_id, cam, vpn_ip)
        if err:
            log.warning("ensure_path %s/%s: %s", device_id, cam, err)
            self._reply(502, _err_body("hls_setup_failed", err, device_id=device_id, camera=cam))
            return

        mtx_name = _mtx_path_name(device_id, cam)
        qs = ""
        if "?" in self.path:
            qs = "?" + self.path.split("?", 1)[1]
        target = f"{MEDIAMTX_HLS}/{mtx_name}{rest}{qs}"

        try:
            req = urllib.request.Request(target, method="GET")
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_SEC) as resp:  # noqa: S310
                payload = resp.read()
                ct = resp.headers.get("Content-Type", "application/octet-stream")
                self._reply(resp.status, payload, content_type=ct)
        except urllib.error.HTTPError as exc:
            payload = exc.read() if exc.fp else b""
            # MediaMTX may 404 until the on-demand RTSP source is ready — soft 503 for Artemis.
            if exc.code == 404:
                self._reply(
                    503,
                    _err_body(
                        "stream_starting",
                        "HLS not ready yet (MediaMTX pulling tower RTSP) — retry shortly",
                        device_id=device_id,
                        camera=cam,
                    ),
                )
                return
            ct = exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json"
            self._reply(
                exc.code,
                payload or _err_body("hls_error", str(exc.reason), device_id=device_id),
                ct,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("HLS forward failed %s: %s", target, exc)
            self._reply(
                503,
                _err_body(
                    "tower_offline",
                    f"HLS unreachable ({type(exc).__name__}) — MediaMTX down or tower RTSP offline",
                    device_id=device_id,
                ),
            )

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    if not HUB_PROXY_TOKEN:
        log.warning("HUB_PROXY_TOKEN is empty — all /hls/* requests will return 401")
    httpd = ThreadingHTTPServer((HUB_HLS_BIND, HUB_HLS_PORT), Handler)
    log.info(
        "listening on %s:%d (mediamtx_api=%s hls=%s)",
        HUB_HLS_BIND, HUB_HLS_PORT, MEDIAMTX_API, MEDIAMTX_HLS,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
