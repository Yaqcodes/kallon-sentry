#!/usr/bin/env python3
"""
Kallon PTZ daemon — long-running ONVIF control for Jetson (systemd).

- Holds one lazily-opened ONVIF session per camera (a pool), keyed by a
  1-based camera index that matches the order of CAMERA_IPS in device.env
  (i.e. `camera` N == mediamtx path `camN`).
- Serves newline-delimited JSON requests over TCP (default 127.0.0.1:8765)
  or a Unix domain socket (--unix PATH, POSIX only).
- Serializes PTZ commands with a lock (one in flight at a time).

Cameras: derived from CAMERA_IPS / CAMERA_RTSP_USER / CAMERA_PASSWORD /
CAMERA_ONVIF_PORT in the environment (device.env). A single camera can also be
given explicitly with --host (bench mode), which becomes camera index 1.

Password: set CAMERA_PASSWORD or pass -p once at startup (not per request).

Protocol (one JSON object per line, UTF-8, trailing \\n):

  Request:  {"id": <any>, "method": "<name>", "params": { ... }}
  Response: {"id": <same>, "ok": true, "result": { ... }}
            {"id": <same>, "ok": false, "error": {"code": "...", "message": "..."}}

Every camera-facing method accepts an optional 1-based "camera" param that
selects which camera to act on. It defaults to 1 when omitted, so existing
single-camera callers keep working unchanged.

Methods:
  ping             — params {}
  list_cameras     — params {} ; result { "cameras": [ {"camera","ip","onvif_port","path"} ] }
  status           — params { "camera"?: int, "profile"?: int }
  move_absolute    — params { "camera"?: int, "pan", "tilt", "zoom"?: number, "profile"?: int,
                               "tolerance"?: float, "poll_ms"?: float, "confirm_timeout"?: float }
                     result { "ok": bool, "round_trip_ms": float }
  move_continuous  — params { "camera"?: int, "pan", "tilt", "zoom", "seconds", "profile"?: int }
  stop             — params { "camera"?: int, "profile"?: int }
  home             — params { "camera"?: int, "profile"?: int }

Example (TCP):
  echo '{"id":1,"method":"ping","params":{}}' | nc -q 1 127.0.0.1 8765
  echo '{"id":2,"method":"list_cameras","params":{}}' | nc -q 1 127.0.0.1 8765
  echo '{"id":3,"method":"move_continuous","params":{"camera":2,"pan":0.3,"tilt":0,"zoom":0,"seconds":0.4}}' | nc -q 1 127.0.0.1 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

from dahua_onvif_control import (
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    DEFAULT_USER,
    connect,
    resolve_password,
    resolve_wsdl_dir,
)
from sentry_ptz_absolute import (
    read_pan_tilt_zoom,
    wait_position_after_command,
    _ptz_service,
    _require_ptz_profile,
)

LOG = logging.getLogger("kallon_ptz_daemon")


class PTZSession:
    """Holds ONVIF camera handle and default profile index."""

    def __init__(self, cam: Any, default_profile: int) -> None:
        self.cam = cam
        self.default_profile = default_profile


@dataclass(frozen=True)
class CameraSpec:
    """Static connection details for one camera (from device.env or --host)."""

    index: int          # 1-based; matches mediamtx path camN and CAMERA_IPS order
    host: str
    onvif_port: int
    user: str
    password: str


class CameraPool:
    """Lazily opens and caches one ONVIF session per camera index.

    Connections are created on first use for a given camera, so a single
    offline camera does not prevent the daemon from serving the others. All
    dispatch happens under the caller's single command lock, so no additional
    locking is required here.
    """

    def __init__(
        self,
        specs: list[CameraSpec],
        timeout: float,
        wsdl_dir: str,
        default_profile: int,
    ) -> None:
        self._specs: dict[int, CameraSpec] = {s.index: s for s in specs}
        self._sessions: dict[int, PTZSession] = {}
        self._timeout = timeout
        self._wsdl_dir = wsdl_dir
        self._default_profile = default_profile

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "camera": s.index,
                "ip": s.host,
                "onvif_port": s.onvif_port,
                "path": f"cam{s.index}",
            }
            for s in sorted(self._specs.values(), key=lambda s: s.index)
        ]

    def get(self, index: int) -> PTZSession:
        spec = self._specs.get(index)
        if spec is None:
            raise ValueError(
                f"unknown camera {index}; known cameras: "
                f"{sorted(self._specs)}"
            )
        session = self._sessions.get(index)
        if session is None:
            session = self._open_session(spec)
            self._sessions[index] = session
        return session

    def _open_session(self, spec: CameraSpec) -> PTZSession:
        cam = connect(
            spec.host,
            spec.onvif_port,
            spec.user,
            spec.password,
            self._timeout,
            self._wsdl_dir,
        )
        profile = self._first_ptz_profile(cam)
        LOG.info(
            "opened ONVIF session camera=%d host=%s:%d profile=%d",
            spec.index,
            spec.host,
            spec.onvif_port,
            profile,
        )
        return PTZSession(cam, profile)

    def invalidate(self, index: int) -> None:
        self._sessions.pop(index, None)

    @staticmethod
    def _first_ptz_profile(cam: Any) -> int:
        """Pick the first media profile that actually has PTZ configured."""
        media = cam.create_media_service()
        for i, prof in enumerate(media.GetProfiles()):
            if prof.PTZConfiguration is not None:
                return i
        return 0


def _profile(params: dict[str, Any], session: PTZSession) -> int:
    v = params.get("profile", session.default_profile)
    if not isinstance(v, int) or v < 0:
        raise ValueError("profile must be a non-negative integer")
    return v


def _continuous_move(cam: Any, profile_index: int, pan: float, tilt: float, zoom: float, seconds: float) -> None:
    ptz = _ptz_service(cam)
    token = _require_ptz_profile(cam, profile_index)

    move = ptz.create_type("ContinuousMove")
    move.ProfileToken = token
    move.Velocity = {"PanTilt": {"x": float(pan), "y": float(tilt)}, "Zoom": {"x": float(zoom)}}
    ptz.ContinuousMove(move)
    time.sleep(max(0.0, float(seconds)))
    stop = ptz.create_type("Stop")
    stop.ProfileToken = token
    stop.PanTilt = True
    stop.Zoom = True
    ptz.Stop(stop)


def _ptz_stop(cam: Any, profile_index: int) -> None:
    ptz = _ptz_service(cam)
    token = _require_ptz_profile(cam, profile_index)
    stop = ptz.create_type("Stop")
    stop.ProfileToken = token
    stop.PanTilt = True
    stop.Zoom = True
    ptz.Stop(stop)


def _ptz_home(cam: Any, profile_index: int) -> None:
    ptz = _ptz_service(cam)
    token = _require_ptz_profile(cam, profile_index)
    try:
        req = ptz.create_type("GotoHomePosition")
        req.ProfileToken = token
        req.Speed = None
        ptz.GotoHomePosition(req)
        return
    except Exception as exc:
        LOG.warning("GotoHomePosition failed (%s); falling back to absolute centre", exc)
    # Dahua cameras often lack a configured ONVIF home — snap to centre instead.
    move = ptz.create_type("AbsoluteMove")
    move.ProfileToken = token
    move.Position = {"PanTilt": {"x": 0.0, "y": 0.0}, "Zoom": {"x": 0.0}}
    move.Speed = {"PanTilt": {"x": 0.5, "y": 0.5}, "Zoom": {"x": 0.5}}
    ptz.AbsoluteMove(move)


def _resolve_camera_index(params: dict[str, Any]) -> int:
    v = params.get("camera", 1)
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise ValueError("camera must be a positive integer (1-based)")
    return v


def dispatch(pool: CameraPool, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "ping":
        return {"pong": True}

    if method == "list_cameras":
        return {"cameras": pool.describe()}

    cam_index = _resolve_camera_index(params)
    session = pool.get(cam_index)

    try:
        return _dispatch_ptz(pool, session, method, params)
    except Exception:
        pool.invalidate(cam_index)
        raise


def _dispatch_ptz(pool: CameraPool, session: PTZSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "status":
        idx = _profile(params, session)
        ptz = _ptz_service(session.cam)
        token = _require_ptz_profile(session.cam, idx)
        pan, tilt, zoom = read_pan_tilt_zoom(ptz, token)
        return {"pan": pan, "tilt": tilt, "zoom": zoom}

    if method == "move_absolute":
        idx = _profile(params, session)
        pan = float(params["pan"])
        tilt = float(params["tilt"])
        zoom = params.get("zoom")
        if zoom is not None:
            zoom = float(zoom)
        tolerance = float(params.get("tolerance", 0.03))
        poll_ms = float(params.get("poll_ms", 50.0))
        confirm_timeout = float(params.get("confirm_timeout", 10.0))
        poll_sec = max(0.001, poll_ms / 1000.0)

        ptz = _ptz_service(session.cam)
        token = _require_ptz_profile(session.cam, idx)
        ok, rtt = wait_position_after_command(
            ptz,
            token,
            pan,
            tilt,
            zoom,
            tolerance=tolerance,
            poll_sec=poll_sec,
            confirm_timeout_sec=confirm_timeout,
        )
        return {"confirmed": ok, "round_trip_ms": rtt * 1000.0}

    if method == "move_continuous":
        idx = _profile(params, session)
        pan = float(params["pan"])
        tilt = float(params["tilt"])
        zoom = float(params["zoom"])
        sec = float(params["seconds"])
        _continuous_move(session.cam, idx, pan, tilt, zoom, sec)
        return {"done": True}

    if method == "stop":
        idx = _profile(params, session)
        _ptz_stop(session.cam, idx)
        return {"done": True}

    if method == "home":
        idx = _profile(params, session)
        _ptz_home(session.cam, idx)
        return {"done": True}

    raise ValueError(f"unknown method: {method}")


def parse_request(line: str) -> tuple[Any, str, dict[str, Any]]:
    obj = json.loads(line)
    rid = obj.get("id")
    method = obj.get("method")
    params = obj.get("params") or {}
    if not isinstance(method, str):
        raise ValueError("method must be a string")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return rid, method, params


def format_response(rid: Any, result: dict[str, Any]) -> str:
    return json.dumps({"id": rid, "ok": True, "result": result}, separators=(",", ":")) + "\n"


def format_error(rid: Any, code: str, message: str) -> str:
    return (
        json.dumps(
            {"id": rid, "ok": False, "error": {"code": code, "message": message}},
            separators=(",", ":"),
        )
        + "\n"
    )


async def client_loop(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    pool: CameraPool,
    cmd_lock: asyncio.Lock,
) -> None:
    peer = writer.get_extra_info("peername")
    while True:
        line_b = await reader.readline()
        if not line_b:
            break
        line = line_b.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        rid: Any = None
        try:
            rid, method, params = parse_request(line)
        except (json.JSONDecodeError, ValueError) as e:
            writer.write(format_error(None, "BAD_REQUEST", str(e)).encode())
            await writer.drain()
            continue

        try:
            async with cmd_lock:
                result = await asyncio.to_thread(dispatch, pool, method, params)
            writer.write(format_response(rid, result).encode())
        except Exception as e:
            LOG.exception("command failed method=%s", method)
            writer.write(format_error(rid, "COMMAND_FAILED", str(e)).encode())
        await writer.drain()

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    LOG.info("client disconnected %s", peer)


async def run_tcp(pool: CameraPool, host: str, port: int) -> None:
    cmd_lock = asyncio.Lock()

    async def _cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await client_loop(reader, writer, pool, cmd_lock)

    server = await asyncio.start_server(_cb, host=host, port=port)
    LOG.info("listening TCP %s:%s", host, port)
    async with server:
        await server.serve_forever()


async def run_unix(pool: CameraPool, path: str) -> None:
    if sys.platform == "win32":
        raise RuntimeError("Unix sockets are not used for this daemon on Windows; use TCP.")

    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    cmd_lock = asyncio.Lock()

    async def _cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await client_loop(reader, writer, pool, cmd_lock)

    server = await asyncio.start_unix_server(_cb, path=path)
    os.chmod(path, 0o600)
    LOG.info("listening Unix socket %s", path)
    async with server:
        await server.serve_forever()


def _build_camera_specs(args: argparse.Namespace, password: str) -> list[CameraSpec]:
    """Build the camera list from --host (bench) or CAMERA_IPS (device.env).

    - Explicit --host wins and yields a single camera at index 1 (bench mode,
      preserves the historical single-camera invocation).
    - Otherwise cameras come from CAMERA_IPS (comma-separated), 1-based in the
      same order used by mediamtx paths cam1..camN. All cameras share
      CAMERA_RTSP_USER / CAMERA_PASSWORD / CAMERA_ONVIF_PORT.
    """
    if args.host:
        return [CameraSpec(1, args.host, args.port, args.user, password)]

    user = os.environ.get("CAMERA_RTSP_USER") or args.user
    try:
        onvif_port = int(os.environ.get("CAMERA_ONVIF_PORT", "") or args.port)
    except ValueError:
        onvif_port = args.port
    ips = [ip.strip() for ip in os.environ.get("CAMERA_IPS", "").split(",") if ip.strip()]
    return [
        CameraSpec(i, ip, onvif_port, user, password)
        for i, ip in enumerate(ips, start=1)
    ]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Kallon ONVIF PTZ daemon (JSON over TCP or Unix socket).")
    parser.add_argument("--listen-host", default=os.environ.get("PTZ_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.environ.get("PTZ_LISTEN_PORT", "8765")),
    )
    parser.add_argument("--unix", default=os.environ.get("PTZ_UNIX_PATH") or None, help="Unix socket path (Linux only)")
    parser.add_argument(
        "--host",
        default=None,
        help="Single camera IP (bench mode); becomes camera 1. "
        "If omitted, cameras are read from CAMERA_IPS in the environment.",
    )
    parser.add_argument("-P", "--port", type=int, default=DEFAULT_PORT, help="Camera ONVIF HTTP port")
    parser.add_argument("-u", "--user", default=DEFAULT_USER)
    parser.add_argument("-p", "--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--profile", type=int, default=0, help="Default media profile index for requests")
    parser.add_argument("--timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT_SEC)
    parser.add_argument("--wsdl-dir", default=None)
    args = parser.parse_args(argv)

    if args.unix and sys.platform == "win32":
        parser.error("--unix is not supported on Windows; use TCP (--listen-host / --listen-port).")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    password = resolve_password(args.password)
    wsdl_dir = resolve_wsdl_dir(args.wsdl_dir)

    specs = _build_camera_specs(args, password)
    if not specs:
        LOG.error(
            "no cameras configured: pass --host or set CAMERA_IPS in the environment."
        )
        return 1

    # Sessions are opened lazily (per camera, on first use) so one offline
    # camera does not stop the daemon from serving the others.
    pool = CameraPool(specs, args.timeout, wsdl_dir, args.profile)
    LOG.info(
        "configured %d camera(s): %s (profile_default=%s)",
        len(specs),
        ", ".join(f"{s.index}:{s.host}:{s.onvif_port}" for s in specs),
        args.profile,
    )

    async def _run() -> None:
        if args.unix:
            await run_unix(pool, args.unix)
        else:
            await run_tcp(pool, args.listen_host, args.listen_port)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        LOG.info("shutdown")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
