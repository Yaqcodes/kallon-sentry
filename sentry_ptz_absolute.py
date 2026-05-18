#!/usr/bin/env python3
"""
Deterministic ONVIF PTZ: AbsoluteMove + GetStatus polling (local LAN, no cloud).

Main actions:
  move        — one absolute move, wait until reported position is within tolerance
  benchmark   — repeat AbsoluteMove between two known poses; measure confirm round-trip
  status      — print current PTZ status once (debug)

Reuses connection defaults and WSDL resolution from dahua_onvif_control.py.
Requires a profile with PTZ and a camera that supports AbsoluteMove (many Dahua PTZ do).

Usage:
  python sentry_ptz_absolute.py move --pan 0.2 --tilt 0
  python sentry_ptz_absolute.py benchmark --count 100
"""

from __future__ import annotations

import argparse
import statistics
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


def _ptz_service(cam: Any) -> Any:
    try:
        return cam.create_ptz_service()
    except Exception as e:
        sys.exit(f"No PTZ service: {e}")


def _require_ptz_profile(cam: Any, profile_index: int) -> str:
    token = profile_token(cam, profile_index)
    media = cam.create_media_service()
    prof = next(p for p in media.GetProfiles() if p.token == token)
    if prof.PTZConfiguration is None:
        sys.exit("This profile has no PTZ. Try another --profile (see dahua_onvif_control.py profiles).")
    return token


def _pan_tilt_zoom_from_position(pos: Any) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if pos is None:
        return None, None, None
    pan_tilt = getattr(pos, "PanTilt", None)
    zoom_v = getattr(pos, "Zoom", None)
    px: Optional[float] = None
    py: Optional[float] = None
    if pan_tilt is not None:
        xv = getattr(pan_tilt, "x", None)
        if xv is None:
            xv = getattr(pan_tilt, "X", None)
        yv = getattr(pan_tilt, "y", None)
        if yv is None:
            yv = getattr(pan_tilt, "Y", None)
        if xv is not None and yv is not None:
            px, py = float(xv), float(yv)
    z: Optional[float] = None
    if zoom_v is not None:
        zv = getattr(zoom_v, "x", None)
        if zv is None:
            zv = getattr(zoom_v, "X", None)
        if zv is not None:
            z = float(zv)
    return px, py, z


def read_pan_tilt_zoom(ptz: Any, token: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    resp = ptz.GetStatus({"ProfileToken": token})
    inner = getattr(resp, "PTZStatus", None) or resp
    pos = getattr(inner, "Position", None)
    return _pan_tilt_zoom_from_position(pos)


def absolute_move(ptz: Any, token: str, pan: float, tilt: float, zoom: Optional[float]) -> None:
    req = ptz.create_type("AbsoluteMove")
    req.ProfileToken = token
    pos: dict[str, Any] = {"PanTilt": {"x": float(pan), "y": float(tilt)}}
    if zoom is not None:
        pos["Zoom"] = {"x": float(zoom)}
    req.Position = pos
    ptz.AbsoluteMove(req)


def wait_position(
    ptz: Any,
    token: str,
    target_pan: float,
    target_tilt: float,
    target_zoom: Optional[float],
    *,
    tolerance: float,
    poll_sec: float,
    confirm_timeout_sec: float,
) -> tuple[bool, float]:
    """
    Poll GetStatus until pan/tilt (and zoom if target_zoom is not None) are within tolerance.
    Returns (ok, elapsed_sec) where elapsed is from first GetStatus after AbsoluteMove... actually
    we measure from immediately before AbsoluteMove is wrong - user wants after move.

    Returns elapsed from start of polling (after AbsoluteMove returns) until match or timeout.
    """
    deadline = time.perf_counter() + confirm_timeout_sec
    t_poll0 = time.perf_counter()
    while time.perf_counter() < deadline:
        sp, st, sz = read_pan_tilt_zoom(ptz, token)
        if sp is not None and st is not None:
            ok_p = abs(sp - target_pan) <= tolerance
            ok_t = abs(st - target_tilt) <= tolerance
            ok_z = True
            if target_zoom is not None:
                ok_z = sz is not None and abs(sz - target_zoom) <= tolerance
            if ok_p and ok_t and ok_z:
                return True, time.perf_counter() - t_poll0
        time.sleep(poll_sec)
    return False, time.perf_counter() - t_poll0


def wait_position_after_command(
    ptz: Any,
    token: str,
    target_pan: float,
    target_tilt: float,
    target_zoom: Optional[float],
    *,
    tolerance: float,
    poll_sec: float,
    confirm_timeout_sec: float,
) -> tuple[bool, float]:
    """Wall-clock from before AbsoluteMove until confirmed (full round-trip)."""
    t0 = time.perf_counter()
    absolute_move(ptz, token, target_pan, target_tilt, target_zoom)
    ok, poll_elapsed = wait_position(
        ptz,
        token,
        target_pan,
        target_tilt,
        target_zoom,
        tolerance=tolerance,
        poll_sec=poll_sec,
        confirm_timeout_sec=confirm_timeout_sec,
    )
    return ok, time.perf_counter() - t0


def cmd_move(
    cam: Any,
    profile_index: int,
    pan: float,
    tilt: float,
    zoom: Optional[float],
    tolerance: float,
    poll_sec: float,
    confirm_timeout_sec: float,
) -> int:
    ptz = _ptz_service(cam)
    token = _require_ptz_profile(cam, profile_index)
    try:
        ok, rtt = wait_position_after_command(
            ptz,
            token,
            pan,
            tilt,
            zoom,
            tolerance=tolerance,
            poll_sec=poll_sec,
            confirm_timeout_sec=confirm_timeout_sec,
        )
    except Exception as e:
        print(f"AbsoluteMove / GetStatus failed: {e}", file=sys.stderr)
        return 2
    if ok:
        print(f"Confirmed within tolerance in {rtt * 1000:.1f} ms (round-trip).")
        return 0
    print(
        f"Timeout: position not within tolerance after {confirm_timeout_sec:.1f}s "
        f"(last RTT measure {rtt * 1000:.1f} ms). Check AbsoluteMove support and ONVIF PTZ spaces.",
        file=sys.stderr,
    )
    return 3


def cmd_status(cam: Any, profile_index: int) -> int:
    ptz = _ptz_service(cam)
    token = _require_ptz_profile(cam, profile_index)
    sp, st, sz = read_pan_tilt_zoom(ptz, token)
    print(f"Reported position: pan={sp!r} tilt={st!r} zoom={sz!r}")
    return 0


def cmd_benchmark(
    cam: Any,
    profile_index: int,
    count: int,
    a_pan: float,
    a_tilt: float,
    b_pan: float,
    b_tilt: float,
    zoom: Optional[float],
    tolerance: float,
    poll_sec: float,
    confirm_timeout_sec: float,
    warmup: bool,
) -> int:
    ptz = _ptz_service(cam)
    token = _require_ptz_profile(cam, profile_index)
    targets = [(a_pan, a_tilt), (b_pan, b_tilt)]

    if warmup:
        print("Warmup: moving to first pose...", flush=True)
        ok, _ = wait_position_after_command(
            ptz, token, a_pan, a_tilt, zoom, tolerance=tolerance, poll_sec=poll_sec, confirm_timeout_sec=confirm_timeout_sec
        )
        if not ok:
            print("Warmup failed to confirm; benchmark may be noisy or invalid.", file=sys.stderr)

    rtts: list[float] = []
    fails = 0
    for i in range(count):
        pan, tilt = targets[i % 2]
        try:
            ok, rtt = wait_position_after_command(
                ptz,
                token,
                pan,
                tilt,
                zoom,
                tolerance=tolerance,
                poll_sec=poll_sec,
                confirm_timeout_sec=confirm_timeout_sec,
            )
        except Exception as e:
            print(f"Iteration {i + 1}: error {e}", file=sys.stderr)
            fails += 1
            continue
        if ok:
            rtts.append(rtt)
        else:
            fails += 1
            print(f"Iteration {i + 1}: confirm timeout", file=sys.stderr)

    n_ok = len(rtts)
    print(f"Iterations: {count}  OK: {n_ok}  Failed/timeouts: {fails}")
    if not rtts:
        print("No successful samples.", file=sys.stderr)
        return 3
    ms = [x * 1000 for x in rtts]
    ms_sorted = sorted(ms)
    p95_idx = min(len(ms_sorted) - 1, int(0.95 * (len(ms_sorted) - 1)))
    print(f"Round-trip confirm time (ms): min={min(ms):.1f}  max={max(ms):.1f}  mean={statistics.mean(ms):.1f}")
    print(f"  median={statistics.median(ms):.1f}  p95~={ms_sorted[p95_idx]:.1f}")
    return 0 if fails == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    acts = ["move", "benchmark", "status"]
    parser = argparse.ArgumentParser(description="AbsoluteMove + GetStatus deterministic PTZ (Sentry Tower).")
    parser.add_argument("action", choices=acts)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("-P", "--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-u", "--user", default=DEFAULT_USER)
    parser.add_argument("-p", "--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--profile", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT_SEC)
    parser.add_argument("--wsdl-dir", default=None)
    parser.add_argument("--tolerance", type=float, default=0.03, help="Max |delta| on each axis vs reported position")
    parser.add_argument("--poll-ms", type=float, default=50.0, help="Sleep between GetStatus polls")
    parser.add_argument("--confirm-timeout", type=float, default=10.0, help="Max seconds to wait for in-tolerance position")

    sub = parser.add_argument_group("move / status")
    sub.add_argument("--pan", type=float, default=0.0)
    sub.add_argument("--tilt", type=float, default=0.0)
    sub.add_argument("--zoom", type=float, default=None, help="If set, included in AbsoluteMove and confirmation")

    bench = parser.add_argument_group("benchmark")
    bench.add_argument("--count", type=int, default=100)
    bench.add_argument("--a-pan", type=float, default=-0.25)
    bench.add_argument("--a-tilt", type=float, default=0.0)
    bench.add_argument("--b-pan", type=float, default=0.25)
    bench.add_argument("--b-tilt", type=float, default=0.0)
    bench.add_argument("--no-warmup", action="store_true", help="Skip initial move to pose A")

    args = parser.parse_args(argv)
    password = resolve_password(args.password)
    wsdl_dir = resolve_wsdl_dir(args.wsdl_dir)
    poll_sec = max(0.001, args.poll_ms / 1000.0)
    z: Optional[float] = args.zoom if args.action in ("move", "benchmark") else None

    try:
        cam = connect(args.host, args.port, args.user, password, args.timeout, wsdl_dir)
    except OSError as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Connect/auth failed: {e}", file=sys.stderr)
        return 1

    if args.action == "move":
        return cmd_move(
            cam,
            args.profile,
            args.pan,
            args.tilt,
            z,
            args.tolerance,
            poll_sec,
            args.confirm_timeout,
        )
    if args.action == "status":
        return cmd_status(cam, args.profile)
    return cmd_benchmark(
        cam,
        args.profile,
        args.count,
        args.a_pan,
        args.a_tilt,
        args.b_pan,
        args.b_tilt,
        z,
        args.tolerance,
        poll_sec,
        args.confirm_timeout,
        warmup=not args.no_warmup,
    )


if __name__ == "__main__":
    raise SystemExit(main())
