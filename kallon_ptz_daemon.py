#!/usr/bin/env python3
"""
Kallon PTZ daemon — long-running ONVIF control for Jetson (systemd).

- Keeps one ONVIF session to the camera.
- Serves newline-delimited JSON requests over TCP (default 127.0.0.1:8765)
  or a Unix domain socket (--unix PATH, POSIX only).
- Serializes PTZ commands with a lock (one in flight at a time).

Password: set CAMERA_PASSWORD or pass -p once at startup (not per request).

Protocol (one JSON object per line, UTF-8, trailing \\n):

  Request:  {"id": <any>, "method": "<name>", "params": { ... }}
  Response: {"id": <same>, "ok": true, "result": { ... }}
            {"id": <same>, "ok": false, "error": {"code": "...", "message": "..."}}

Methods:
  ping             — params {}
  status           — params { "profile"?: int }
  move_absolute    — params { "pan", "tilt", "zoom"?: number, "profile"?: int,
                               "tolerance"?: float, "poll_ms"?: float, "confirm_timeout"?: float }
                     result { "ok": bool, "round_trip_ms": float }
  move_continuous  — params { "pan", "tilt", "zoom", "seconds", "profile"?: int }
  stop             — params { "profile"?: int }
  home             — params { "profile"?: int }

Example (TCP):
  echo '{"id":1,"method":"ping","params":{}}' | nc -q 1 127.0.0.1 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Optional

from dahua_onvif_control import (
    DEFAULT_HOST,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    DEFAULT_USER,
    connect,
    profile_token,
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


def _profile(params: dict[str, Any], session: PTZSession) -> int:
    v = params.get("profile", session.default_profile)
    if not isinstance(v, int) or v < 0:
        raise ValueError("profile must be a non-negative integer")
    return v


def _continuous_move(cam: Any, profile_index: int, pan: float, tilt: float, zoom: float, seconds: float) -> None:
    ptz = _ptz_service(cam)
    media = cam.create_media_service()
    token = profile_token(cam, profile_index)
    profile = next(p for p in media.GetProfiles() if p.token == token)
    if profile.PTZConfiguration is None:
        raise RuntimeError("This profile has no PTZ")

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
    token = profile_token(cam, profile_index)
    stop = ptz.create_type("Stop")
    stop.ProfileToken = token
    stop.PanTilt = True
    stop.Zoom = True
    ptz.Stop(stop)


def _ptz_home(cam: Any, profile_index: int) -> None:
    ptz = _ptz_service(cam)
    token = profile_token(cam, profile_index)
    req = ptz.create_type("GotoHomePosition")
    req.ProfileToken = token
    req.Speed = None
    ptz.GotoHomePosition(req)


def dispatch(session: PTZSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "ping":
        return {"pong": True}

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
    session: PTZSession,
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
                result = await asyncio.to_thread(dispatch, session, method, params)
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


async def run_tcp(session: PTZSession, host: str, port: int) -> None:
    cmd_lock = asyncio.Lock()

    async def _cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await client_loop(reader, writer, session, cmd_lock)

    server = await asyncio.start_server(_cb, host=host, port=port)
    LOG.info("listening TCP %s:%s", host, port)
    async with server:
        await server.serve_forever()


async def run_unix(session: PTZSession, path: str) -> None:
    if sys.platform == "win32":
        raise RuntimeError("Unix sockets are not used for this daemon on Windows; use TCP.")

    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    cmd_lock = asyncio.Lock()

    async def _cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await client_loop(reader, writer, session, cmd_lock)

    server = await asyncio.start_unix_server(_cb, path=path)
    os.chmod(path, 0o600)
    LOG.info("listening Unix socket %s", path)
    async with server:
        await server.serve_forever()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Kallon ONVIF PTZ daemon (JSON over TCP or Unix socket).")
    parser.add_argument("--listen-host", default=os.environ.get("PTZ_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.environ.get("PTZ_LISTEN_PORT", "8765")),
    )
    parser.add_argument("--unix", default=os.environ.get("PTZ_UNIX_PATH") or None, help="Unix socket path (Linux only)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Camera IP")
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

    try:
        cam = connect(args.host, args.port, args.user, password, args.timeout, wsdl_dir)
    except OSError as e:
        LOG.error("network error: %s", e)
        return 1
    except Exception as e:
        LOG.error("connect/auth failed: %s", e)
        return 1

    session = PTZSession(cam, args.profile)
    LOG.info("connected to camera %s:%s profile_default=%s", args.host, args.port, args.profile)

    async def _run() -> None:
        if args.unix:
            await run_unix(session, args.unix)
        else:
            await run_tcp(session, args.listen_host, args.listen_port)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        LOG.info("shutdown")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
