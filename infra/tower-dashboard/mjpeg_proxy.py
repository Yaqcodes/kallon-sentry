#!/usr/bin/env python3
"""kallon-mjpeg-proxy — near-real-time MJPEG over HTTP for the kiosk dashboard.

Pulls RTSP from the local mediamtx rebroadcast (:8554) via ffmpeg and pushes
frames as multipart/x-mixed-replace to connected browser clients.  All traffic
stays on loopback (127.0.0.1); this service must never be exposed to the network.

Offline cameras stay idle: a lightweight poll against the mediamtx Control API
detects when a path becomes ready, so plugging a camera in resumes video within
one poll interval — without crash-looping ffmpeg on dead RTSP paths.

Environment (all optional):
  MJPEG_BIND         bind host           (default: 127.0.0.1)
  MJPEG_PORT         TCP port            (default: 8889)
  RTSP_BASE          mediamtx loopback   (default: rtsp://127.0.0.1:8554)
  MEDIAMTX_API       control API         (default: http://127.0.0.1:9997)
  CAMERA_IPS         comma-separated IPs — used only to derive camera count
  MJPEG_FPS          output fps          (default: 15)
  MJPEG_QUALITY      ffmpeg -q:v 1-31, lower=better  (default: 8)
  MJPEG_SCALE        ffmpeg scale filter (default: 1280:-2)
  MJPEG_DECODER      ffmpeg input decoder (default: h264_nvv4l2dec on aarch64; empty=software)
  MJPEG_HWACCEL      deprecated alias — if set to nvv4l2dec, maps to h264_nvv4l2dec
  MJPEG_READY_POLL   seconds between mediamtx readiness checks (default: 5)
  MJPEG_IDLE_BACKOFF seconds to sleep after ffmpeg exit when path offline (default: 5)

Routes:
  GET /camN    → multipart/x-mixed-replace MJPEG stream
  GET /healthz → 200 {"ok": true}
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Dict, Optional, Set

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [mjpeg-proxy] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("mjpeg-proxy")

BIND_HOST  = os.environ.get("MJPEG_BIND", "127.0.0.1")
MJPEG_PORT = int(os.environ.get("MJPEG_PORT", "8889"))
RTSP_BASE  = os.environ.get("RTSP_BASE", "rtsp://127.0.0.1:8554").rstrip("/")
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997").rstrip("/")
MJPEG_FPS  = int(os.environ.get("MJPEG_FPS", "15"))
MJPEG_Q    = int(os.environ.get("MJPEG_QUALITY", "8"))
MJPEG_SCALE = os.environ.get("MJPEG_SCALE", "1280:-2")


def _resolve_decoder() -> str:
    """Jetson ffmpeg uses -c:v h264_nvv4l2dec, not -hwaccel nvv4l2dec."""
    if "MJPEG_DECODER" in os.environ:
        return os.environ.get("MJPEG_DECODER", "").strip()
    legacy = os.environ.get("MJPEG_HWACCEL", "").strip()
    if legacy in ("", "0", "no", "false"):
        pass
    elif legacy == "nvv4l2dec":
        return "h264_nvv4l2dec"
    else:
        return legacy
    if os.uname().machine in ("aarch64", "arm64"):
        return "h264_nvv4l2dec"
    return ""


DECODER = _resolve_decoder()
READY_POLL = float(os.environ.get("MJPEG_READY_POLL", "5"))
IDLE_BACKOFF = float(os.environ.get("MJPEG_IDLE_BACKOFF", "5"))

_raw_ips     = os.environ.get("CAMERA_IPS", "cam1")
CAMERA_PATHS = [f"cam{i+1}" for i in range(len([x for x in _raw_ips.split(",") if x.strip()]))]

BOUNDARY = b"--frame"
CRLF     = b"\r\n"
SOI      = b"\xff\xd8"
EOI      = b"\xff\xd9"


def _mediamtx_ready(path: str) -> bool:
    """Ask mediamtx whether a path is currently publishing (cheap HTTP GET)."""
    try:
        req = urllib.request.Request(
            f"{MEDIAMTX_API}/v3/paths/get/{path}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("ready"))
    except Exception:
        return False


class FrameBroadcaster:
    """Per-camera capture: idle when offline, ffmpeg only while ready + needed."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.url = f"{RTSP_BASE}/{path}"
        self._lock = threading.Lock()
        self._subs: Set[queue.Queue] = set()
        self._running = True
        self._ready = False
        self._proc: Optional[subprocess.Popen] = None
        t = threading.Thread(target=self._capture_loop, daemon=True, name=f"cap-{path}")
        t.start()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def _subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def _broadcast(self, frame: bytes) -> None:
        with self._lock:
            stale = []
            for q in self._subs:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    stale.append(q)
            for q in stale:
                self._subs.discard(q)

    def _stop_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    def _build_cmd(self) -> list[str]:
        vf = f"scale={MJPEG_SCALE},fps={MJPEG_FPS}" if MJPEG_SCALE else f"fps={MJPEG_FPS}"
        cmd = ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp"]
        if DECODER:
            cmd += ["-c:v", DECODER]
        cmd += [
            "-i", self.url,
            "-vf", vf,
            "-q:v", str(MJPEG_Q),
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-",
        ]
        return cmd

    def _capture_loop(self) -> None:
        last_ready_log = 0.0
        while self._running:
            self._ready = _mediamtx_ready(self.path)
            if not self._ready:
                self._stop_proc()
                now = time.monotonic()
                if now - last_ready_log > 60:
                    log.info("%s offline — idle (no ffmpeg)", self.path)
                    last_ready_log = now
                time.sleep(READY_POLL)
                continue

            if self._subscriber_count() == 0:
                self._stop_proc()
                time.sleep(READY_POLL)
                continue

            log.info("starting capture for %s", self.path)
            try:
                self._proc = subprocess.Popen(
                    self._build_cmd(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                proc = self._proc
                buf = b""
                while self._running and proc.poll() is None:
                    if self._subscriber_count() == 0:
                        break
                    if not _mediamtx_ready(self.path):
                        log.info("%s went offline — stopping ffmpeg", self.path)
                        break
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(SOI)
                        if start == -1:
                            buf = b""
                            break
                        end = buf.find(EOI, start + 2)
                        if end == -1:
                            buf = buf[start:]
                            break
                        self._broadcast(buf[start : end + 2])
                        buf = buf[end + 2 :]
            except Exception as exc:
                log.warning("capture error for %s: %s", self.path, exc)
            finally:
                self._stop_proc()

            if not self._running:
                break
            time.sleep(IDLE_BACKOFF)


_broadcasters: Dict[str, FrameBroadcaster] = {}


class MjpegHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # suppress per-request access log
        pass

    def do_GET(self):
        path = self.path.lstrip("/").split("?")[0]

        if path == "healthz":
            self._json({
                "ok": True,
                "cameras": {
                    p: {"ready": _mediamtx_ready(p), "subscribers": _broadcasters[p]._subscriber_count()}
                    for p in _broadcasters
                },
            })
            return

        bc = _broadcasters.get(path)
        if bc is None:
            self.send_error(404, f"Unknown camera path: {path!r}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()

        q = bc.subscribe()
        try:
            while True:
                try:
                    frame = q.get(timeout=15)
                except queue.Empty:
                    if not _mediamtx_ready(path):
                        break
                    continue
                header = (
                    BOUNDARY + CRLF
                    + b"Content-Type: image/jpeg" + CRLF
                    + f"Content-Length: {len(frame)}".encode() + CRLF
                    + CRLF
                )
                self.wfile.write(header + frame + CRLF)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:
            log.debug("client %s disconnected: %s", self.client_address, exc)
        finally:
            bc.unsubscribe(q)

    def do_HEAD(self):
        path = self.path.lstrip("/").split("?")[0]
        if path == "healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        if path in _broadcasters:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
            self.end_headers()
            return
        self.send_error(404)

    def _json(self, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main() -> None:
    for path in CAMERA_PATHS:
        _broadcasters[path] = FrameBroadcaster(path)
    log.info("broadcasters for: %s (poll=%.0fs, fps=%d, decoder=%s)", ", ".join(CAMERA_PATHS), READY_POLL, MJPEG_FPS, DECODER or "software")

    srv = _ThreadingHTTPServer((BIND_HOST, MJPEG_PORT), MjpegHandler)
    log.info("listening on %s:%d", BIND_HOST, MJPEG_PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        srv.server_close()


if __name__ == "__main__":
    main()
