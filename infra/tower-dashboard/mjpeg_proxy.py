#!/usr/bin/env python3
"""kallon-mjpeg-proxy — near-real-time MJPEG over HTTP for the kiosk dashboard.

Pulls RTSP from the local mediamtx rebroadcast (:8554) via ffmpeg and pushes
frames as multipart/x-mixed-replace to connected browser clients.  All traffic
stays on loopback (127.0.0.1); this service must never be exposed to the network.

Environment (all optional):
  MJPEG_BIND      bind host           (default: 127.0.0.1)
  MJPEG_PORT      TCP port            (default: 8889)
  RTSP_BASE       mediamtx loopback   (default: rtsp://127.0.0.1:8554)
  CAMERA_IPS      comma-separated IPs — used only to derive camera count
  MJPEG_FPS       output fps          (default: 10)
  MJPEG_QUALITY   ffmpeg -q:v 1-31, lower=better  (default: 5)
  MJPEG_HWACCEL   ffmpeg -hwaccel arg (default: auto)

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
from typing import Dict, Set

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [mjpeg-proxy] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("mjpeg-proxy")

BIND_HOST  = os.environ.get("MJPEG_BIND",    "127.0.0.1")
MJPEG_PORT = int(os.environ.get("MJPEG_PORT", "8889"))
RTSP_BASE  = os.environ.get("RTSP_BASE",      "rtsp://127.0.0.1:8554").rstrip("/")
MJPEG_FPS  = int(os.environ.get("MJPEG_FPS",  "10"))
MJPEG_Q    = int(os.environ.get("MJPEG_QUALITY", "5"))
HWACCEL    = os.environ.get("MJPEG_HWACCEL", "auto")

_raw_ips     = os.environ.get("CAMERA_IPS", "cam1")
CAMERA_PATHS = [f"cam{i+1}" for i in range(len([x for x in _raw_ips.split(",") if x.strip()]))]

BOUNDARY = b"--frame"
CRLF     = b"\r\n"
SOI      = b"\xff\xd8"   # JPEG start-of-image
EOI      = b"\xff\xd9"   # JPEG end-of-image


class FrameBroadcaster:
    """One persistent ffmpeg process per camera; fans latest frames to HTTP clients."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.url  = f"{RTSP_BASE}/{path}"
        self._lock = threading.Lock()
        self._subs: Set[queue.Queue] = set()
        self._running = True
        t = threading.Thread(target=self._capture_loop, daemon=True, name=f"cap-{path}")
        t.start()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=3)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

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

    def _capture_loop(self) -> None:
        while self._running:
            cmd = [
                "ffmpeg", "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-hwaccel", HWACCEL,
                "-i", self.url,
                "-vf", f"fps={MJPEG_FPS}",
                "-q:v", str(MJPEG_Q),
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "-",
            ]
            log.info("starting capture for %s", self.path)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                buf = b""
                while self._running:
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
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:
                log.warning("capture error for %s: %s", self.path, exc)
            if self._running:
                log.info("ffmpeg for %s exited, retrying in 3s", self.path)
                time.sleep(3)


_broadcasters: Dict[str, FrameBroadcaster] = {}


class MjpegHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # suppress per-request access log
        pass

    def do_GET(self):
        path = self.path.lstrip("/").split("?")[0]

        if path == "healthz":
            self._json({"ok": True, "cameras": list(_broadcasters)})
            return

        bc = _broadcasters.get(path)
        if bc is None:
            self.send_error(404, f"Unknown camera path: {path!r}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = bc.subscribe()
        try:
            while True:
                try:
                    frame = q.get(timeout=10)
                except queue.Empty:
                    continue
                header = (
                    BOUNDARY + CRLF
                    + b"Content-Type: image/jpeg" + CRLF
                    + f"Content-Length: {len(frame)}".encode() + CRLF
                    + CRLF
                )
                self.wfile.write(header + frame + CRLF)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.debug("client %s disconnected: %s", self.client_address, exc)
        finally:
            bc.unsubscribe(q)

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
    log.info("broadcasters started for: %s", ", ".join(CAMERA_PATHS))

    srv = _ThreadingHTTPServer((BIND_HOST, MJPEG_PORT), MjpegHandler)
    log.info("listening on %s:%d  fps=%d  quality=%d", BIND_HOST, MJPEG_PORT, MJPEG_FPS, MJPEG_Q)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        srv.server_close()


if __name__ == "__main__":
    main()
