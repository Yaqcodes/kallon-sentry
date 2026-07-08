#!/usr/bin/env python3
"""Sentinel — tower dashboard ingest gateway (loopback only).

A thin, stdlib-only HTTP server for the OPTIONAL on-Jetson Sentinel dashboard. It
does not read any hardware and contains no monitoring logic of its own — it
only *ingests* from the surfaces that already exist on the tower and fans the
data out to a local browser:

  - mediamtx Control API (127.0.0.1:9997)  -> per-camera stream readiness
  - mediamtx HLS (127.0.0.1:8888)          -> the browser plays these directly
  - watchdog status API (127.0.0.1:8770)   -> live sensor / health snapshot
  - watchdog alerts, mirrored via the local alert_listener -> POST /ingest/alerts
  - PTZ daemon (127.0.0.1:8765)            -> PTZ button commands (relayed)

Everything is bound to 127.0.0.1: this is a bench tool for a monitor plugged
directly into the Jetson, never a network service. It is entirely separate
from the Terra buyer dashboard (see docs/alert-webhook.md).

Since July 2026 the gateway is ALSO the internal proxy target for the Terra
control plane's platform API (docs/platform-api.md): when DASH_BIND=wg0 it
binds the WireGuard interface address (keeping a loopback listener for the
local SPA) so the control plane can reach it over the VPN. It gains REST PTZ
endpoints and a snapshot endpoint for that purpose. SDK consumers never call
this service directly — only the control plane does.

Endpoints
---------
  GET  /                      -> static SPA (index.html)
  GET  /<static asset>        -> files under WEB_ROOT
  GET  /healthz               -> {"status": "ok"}
  GET  /api/config            -> device id + camera list (from device.env)
  GET  /api/streams           -> mediamtx path readiness (proxied/simplified)
  GET  /api/status            -> watchdog status snapshot (proxied)
  GET  /api/events            -> Server-Sent Events stream of alerts
  GET  /api/snapshot/cam<n>   -> single JPEG frame (ffmpeg, local RTSP)
  GET  /api/ptz/status        -> current pan/tilt/zoom (?camera=n)
  POST /api/ptz/move          -> absolute/continuous move (REST shape)
  POST /api/ptz/stop          -> stop (or home with {"home": true})
  POST /ingest/alerts         -> receive a forwarded alert (from alert_listener)
  POST /api/ptz               -> legacy relay used by the on-Jetson SPA

Environment
-----------
  DASH_BIND            default 127.0.0.1. May be an IP or an interface name
                       (e.g. "wg0") which is resolved to its IPv4 address at
                       startup; a loopback listener is kept alongside.
  DASH_PORT            default 8766
  RTSP_LOCAL_BASE      default rtsp://127.0.0.1:8554 (snapshot source)
  SNAPSHOT_TIMEOUT_SEC default 15 (ffmpeg frame-capture budget)
  WEB_ROOT             default <this dir>/web
  MEDIAMTX_API         default http://127.0.0.1:9997
  MEDIAMTX_HLS         default http://127.0.0.1:8888   (advertised to the browser)
  WATCHDOG_STATUS_URL  default http://127.0.0.1:8770
  PTZ_HOST             default 127.0.0.1
  PTZ_PORT             default 8765
  ALERT_HISTORY        default 200         (in-memory ring buffer size)
  CAMERA_IPS           from device.env (comma-separated) -> camera count
  DEVICE_ID            from device.env
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tower-dashboard")

DASH_BIND = os.environ.get("DASH_BIND", "127.0.0.1")
DASH_PORT = int(os.environ.get("DASH_PORT", "8766"))
WEB_ROOT = Path(os.environ.get("WEB_ROOT", str(Path(__file__).resolve().parent / "web")))
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997").rstrip("/")
MEDIAMTX_HLS = os.environ.get("MEDIAMTX_HLS", "http://127.0.0.1:8888").rstrip("/")
MJPEG_PROXY  = os.environ.get("MJPEG_PROXY",  "http://127.0.0.1:8889").rstrip("/")
WATCHDOG_STATUS_URL = os.environ.get("WATCHDOG_STATUS_URL", "http://127.0.0.1:8770").rstrip("/")
PTZ_HOST = os.environ.get("PTZ_HOST", "127.0.0.1")
PTZ_PORT = int(os.environ.get("PTZ_PORT", "8765"))
ALERT_HISTORY = int(os.environ.get("ALERT_HISTORY", "200"))
RTSP_LOCAL_BASE = os.environ.get("RTSP_LOCAL_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
SNAPSHOT_TIMEOUT_SEC = float(os.environ.get("SNAPSHOT_TIMEOUT_SEC", "15"))

_SNAPSHOT_RE = re.compile(r"^/api/snapshot/cam(\d+)$")

PTZ_METHODS = {
    "ping",
    "list_cameras",
    "status",
    "move_absolute",
    "move_continuous",
    "stop",
    "home",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


# ---------------------------------------------------------------------------
# Alert bus: in-memory ring buffer + SSE subscriber fan-out
# ---------------------------------------------------------------------------


class AlertBus:
    """Keeps the most recent alerts and pushes new ones to SSE subscribers."""

    def __init__(self, history: int) -> None:
        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = []
        self._max = history
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    def publish(self, alert: dict[str, Any]) -> None:
        with self._lock:
            self._history.append(alert)
            if len(self._history) > self._max:
                self._history = self._history[-self._max :]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(alert)
            except queue.Full:  # pragma: no cover - unbounded queues used
                pass

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)

    def subscribe(self) -> "queue.Queue[dict[str, Any]]":
        q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue[dict[str, Any]]") -> None:
        with self._lock:
            self._subscribers.discard(q)


BUS = AlertBus(ALERT_HISTORY)


# ---------------------------------------------------------------------------
# Alert normalisation
# ---------------------------------------------------------------------------

_SEVERITY_ALIASES = {"info": "info", "warning": "warning", "critical": "critical"}


def normalize_alert(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a watchdog alert into a stable shape the UI can rely on.

    The watchdog emits UPPERCASE alert_type / severity; older docs used
    lowercase. We keep the original but add lowercased convenience fields and
    an ingest timestamp so the UI never has to special-case casing.
    """
    alert_type = str(raw.get("alert_type") or raw.get("type") or "UNKNOWN")
    severity = str(raw.get("severity") or "").lower() or "info"
    severity = _SEVERITY_ALIASES.get(severity, severity)
    return {
        "device_id": raw.get("device_id"),
        "alert_type": alert_type,
        "kind": alert_type.lower(),
        "severity": severity,
        "timestamp_utc": raw.get("timestamp_utc"),
        "nonce": raw.get("nonce"),
        "details": raw.get("details") or {},
        "received_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Upstream helpers (all loopback)
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = 4.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback)
        return json.loads(resp.read().decode("utf-8"))


def camera_list() -> list[dict[str, Any]]:
    ips = [ip.strip() for ip in os.environ.get("CAMERA_IPS", "").split(",") if ip.strip()]
    cameras = []
    for i, ip in enumerate(ips, start=1):
        cameras.append(
            {
                "camera": i,
                "path": f"cam{i}",
                "label": f"cam{i}",
                "ip": ip,
                "hls_url": f"{MEDIAMTX_HLS}/cam{i}/index.m3u8",
                "mjpeg_url": f"{MJPEG_PROXY}/cam{i}" if MJPEG_PROXY else None,
            }
        )
    return cameras


def ptz_command(payload: dict[str, Any]) -> dict[str, Any]:
    method = payload.get("method")
    if method not in PTZ_METHODS:
        return {"ok": False, "error": {"code": "BAD_METHOD", "message": f"method {method!r} not allowed"}}
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return {"ok": False, "error": {"code": "BAD_PARAMS", "message": "params must be an object"}}
    # Allow a top-level "camera" for convenience; fold it into params.
    if "camera" in payload and "camera" not in params:
        params["camera"] = payload["camera"]
    request_line = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
    try:
        with socket.create_connection((PTZ_HOST, PTZ_PORT), timeout=10.0) as sock:
            sock.sendall(request_line.encode("utf-8"))
            sock.settimeout(10.0)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        return {"ok": False, "error": {"code": "PTZ_UNREACHABLE", "message": str(exc)}}
    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if not line:
        return {"ok": False, "error": {"code": "PTZ_NO_RESPONSE", "message": "empty response"}}
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": {"code": "PTZ_BAD_JSON", "message": str(exc)}}


def snapshot_jpeg(camera: int) -> tuple[Optional[bytes], Optional[dict[str, Any]]]:
    """Capture one JPEG frame from the local RTSP rebroadcast via ffmpeg.

    Returns (jpeg_bytes, None) on success or (None, error_object) on failure.
    """
    n_cameras = len(camera_list())
    if camera < 1 or (n_cameras and camera > n_cameras):
        return None, {"code": "not_found", "message": f"camera {camera} out of range (1..{n_cameras})"}
    url = f"{RTSP_LOCAL_BASE}/cam{camera}"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", url,
        "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=SNAPSHOT_TIMEOUT_SEC, check=False
        )
    except FileNotFoundError:
        return None, {"code": "tower_error", "message": "ffmpeg not installed on tower"}
    except subprocess.TimeoutExpired:
        return None, {"code": "tower_error", "message": f"snapshot timed out after {SNAPSHOT_TIMEOUT_SEC}s"}
    if proc.returncode != 0 or not proc.stdout:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
        return None, {"code": "tower_error", "message": f"ffmpeg failed: {stderr or 'no output'}"}
    return proc.stdout, None


def ptz_rest_move(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """REST-shaped PTZ move -> daemon call. Returns (http_status, body)."""
    mode = payload.get("mode", "absolute")
    camera = payload.get("camera", 1)
    if mode == "absolute":
        params: dict[str, Any] = {"camera": camera}
        for k in ("pan", "tilt"):
            if k not in payload:
                return 422, {"error": {"code": "invalid_request", "message": f"'{k}' required for absolute move"}}
            params[k] = payload[k]
        if payload.get("zoom") is not None:
            params["zoom"] = payload["zoom"]
        method = "move_absolute"
    elif mode == "continuous":
        params = {"camera": camera}
        for k in ("pan", "tilt", "zoom", "seconds"):
            if k not in payload:
                return 422, {"error": {"code": "invalid_request", "message": f"'{k}' required for continuous move"}}
            params[k] = payload[k]
        method = "move_continuous"
    else:
        return 422, {"error": {"code": "invalid_request", "message": f"unknown mode {mode!r} (absolute|continuous)"}}
    resp = ptz_command({"method": method, "params": params})
    if not resp.get("ok", False):
        return 502, {"error": {"code": "tower_error", "message": "PTZ daemon error", "ptz": resp.get("error")}}
    return 200, {"ok": True, "result": resp.get("result", {})}


def resolve_bind(bind: str) -> str:
    """Resolve DASH_BIND: an IP is returned as-is; an interface name (e.g.
    'wg0') is resolved to its IPv4 address so the gateway can serve the VPN.
    """
    try:
        socket.inet_aton(bind)
        return bind
    except OSError:
        pass
    # Interface name — resolve via `ip -4 addr show <iface>` (Linux towers).
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", bind],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    log.warning("could not resolve DASH_BIND=%r to an address; falling back to 127.0.0.1", bind)
    return "127.0.0.1"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "kallon-tower-dashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- small helpers -----------------------------------------------------
    def _send(self, code: int, body: bytes, content_type: str, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def log_message(self, *args: Any) -> None:  # keep the journal quiet
        pass

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/api/config":
            self._json(200, {
                "device_id": os.environ.get("DEVICE_ID", ""),
                "cameras": camera_list(),
                "hls_base": MEDIAMTX_HLS,
            })
        elif path == "/api/streams":
            self._streams()
        elif path == "/api/status":
            self._status()
        elif path == "/api/events":
            self._events()
        elif path == "/api/ptz/status":
            self._ptz_status()
        elif _SNAPSHOT_RE.match(path):
            self._snapshot(int(_SNAPSHOT_RE.match(path).group(1)))  # type: ignore[union-attr]
        else:
            self._static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/ingest/alerts":
            self._ingest()
        elif path == "/api/ptz":
            self._ptz()
        elif path == "/api/ptz/move":
            self._ptz_move()
        elif path == "/api/ptz/stop":
            self._ptz_stop()
        else:
            self._json(404, {"error": {"code": "not_found", "message": f"no route {path}"}})

    # -- endpoint impls ----------------------------------------------------
    def _streams(self) -> None:
        try:
            data = _http_get_json(f"{MEDIAMTX_API}/v3/paths/list")
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"available": False, "error": str(exc), "paths": []})
            return
        items = data.get("items", []) if isinstance(data, dict) else []
        paths = [
            {
                "name": it.get("name"),
                "ready": bool(it.get("ready")),
                "readers": len(it.get("readers", []) or []),
                "source": (it.get("source") or {}).get("type") if it.get("source") else None,
            }
            for it in items
        ]
        self._json(200, {"available": True, "paths": paths})

    def _status(self) -> None:
        try:
            snap = _http_get_json(f"{WATCHDOG_STATUS_URL}/status")
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"available": False, "error": str(exc)})
            return
        snap["available"] = True
        self._json(200, snap)

    def _ingest(self) -> None:
        try:
            raw = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        alert = normalize_alert(raw if isinstance(raw, dict) else {})
        BUS.publish(alert)
        log.info("ingested alert type=%s severity=%s", alert["alert_type"], alert["severity"])
        self._json(200, {"status": "accepted"})

    def _ptz(self) -> None:
        try:
            payload = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "body must be an object"})
            return
        self._json(200, ptz_command(payload))

    # -- REST PTZ + snapshot (platform proxy surface) ------------------------
    def _read_json_object(self) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            self._json(422, {"error": {"code": "invalid_request", "message": "body is not valid JSON"}})
            return None
        if not isinstance(payload, dict):
            self._json(422, {"error": {"code": "invalid_request", "message": "body must be a JSON object"}})
            return None
        return payload

    def _ptz_move(self) -> None:
        payload = self._read_json_object()
        if payload is None:
            return
        status, body = ptz_rest_move(payload)
        self._json(status, body)

    def _ptz_stop(self) -> None:
        payload = self._read_json_object()
        if payload is None:
            return
        method = "home" if payload.get("home") else "stop"
        resp = ptz_command({"method": method, "params": {"camera": payload.get("camera", 1)}})
        if not resp.get("ok", False):
            self._json(502, {"error": {"code": "tower_error", "message": "PTZ daemon error", "ptz": resp.get("error")}})
            return
        self._json(200, {"ok": True, "result": resp.get("result", {})})

    def _ptz_status(self) -> None:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        camera = 1
        for part in query.split("&"):
            if part.startswith("camera="):
                try:
                    camera = int(part.split("=", 1)[1])
                except ValueError:
                    self._json(422, {"error": {"code": "invalid_request", "message": "camera must be an integer"}})
                    return
        resp = ptz_command({"method": "status", "params": {"camera": camera}})
        if not resp.get("ok", False):
            self._json(502, {"error": {"code": "tower_error", "message": "PTZ daemon error", "ptz": resp.get("error")}})
            return
        self._json(200, {"ok": True, "result": resp.get("result", {})})

    def _snapshot(self, camera: int) -> None:
        jpeg, err = snapshot_jpeg(camera)
        if err is not None:
            status = 404 if err.get("code") == "not_found" else 502
            self._json(status, {"error": err})
            return
        assert jpeg is not None
        self._send(200, jpeg, "image/jpeg")

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = BUS.subscribe()
        try:
            # Replay recent history so a freshly-opened page is populated.
            for alert in BUS.recent():
                self._sse_write(alert)
            while True:
                try:
                    alert = q.get(timeout=15.0)
                    self._sse_write(alert)
                except queue.Empty:
                    # Heartbeat comment keeps the connection (and proxies) alive.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)

    def _sse_write(self, alert: dict[str, Any]) -> None:
        data = json.dumps(alert)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (WEB_ROOT / rel).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self._json(403, {"error": "forbidden"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            # SPA fallback: unknown routes serve index.html.
            target = WEB_ROOT / "index.html"
            if not target.is_file():
                self._json(404, {"error": "not found"})
                return
        body = target.read_bytes()
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, body, ctype)


def main() -> None:
    bind = resolve_bind(DASH_BIND)
    httpd = ThreadingHTTPServer((bind, DASH_PORT), Handler)
    httpd.daemon_threads = True
    # When bound to a VPN address (platform proxy mode), keep a loopback
    # listener too so the on-Jetson SPA / kiosk keeps working unchanged.
    lo_httpd: Optional[ThreadingHTTPServer] = None
    if bind != "127.0.0.1":
        try:
            lo_httpd = ThreadingHTTPServer(("127.0.0.1", DASH_PORT), Handler)
            lo_httpd.daemon_threads = True
            threading.Thread(target=lo_httpd.serve_forever, daemon=True).start()
        except OSError as exc:
            log.warning("loopback listener unavailable: %s", exc)
    log.info(
        "tower dashboard gateway on %s:%d (bind=%s, web_root=%s, mediamtx_api=%s, hls=%s, mjpeg=%s, status=%s, ptz=%s:%d)",
        bind, DASH_PORT, DASH_BIND, WEB_ROOT, MEDIAMTX_API, MEDIAMTX_HLS, MJPEG_PROXY,
        WATCHDOG_STATUS_URL, PTZ_HOST, PTZ_PORT,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
        if lo_httpd:
            lo_httpd.shutdown()


if __name__ == "__main__":
    main()
