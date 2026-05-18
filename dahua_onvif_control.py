#!/usr/bin/env python3
"""
Control a Dahua IP camera over ONVIF using onvif-zeep.

Prerequisites:
  pip install -r requirements.txt
  On the camera web UI, enable ONVIF (often under Network → Platform Access).

Usage (defaults: host 192.168.1.108, user admin, password from script or env):
  python dahua_onvif_control.py
  python dahua_onvif_control.py -p other_password
  set CAMERA_PASSWORD=other_password && python dahua_onvif_control.py ptz --pan 0.25

Edit DEFAULT_HOST / DEFAULT_PASSWORD below if your setup changes.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from typing import Any

from onvif import ONVIFCamera
from zeep.transports import Transport

# --- defaults for your network (change here if needed) ---
DEFAULT_HOST = "192.168.1.108"
DEFAULT_PORT = 80
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "terra123"
# How long each ONVIF HTTP call may take before failing (helps avoid hangs)
DEFAULT_REQUEST_TIMEOUT_SEC = 15.0


def resolve_wsdl_dir(cli_wsdl_dir: str | None) -> str:
    """PyPI wheels for onvif-zeep often omit WSDL files; prefer bundled CODE/wsdl."""
    if cli_wsdl_dir:
        root = os.path.abspath(cli_wsdl_dir)
    else:
        bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wsdl")
        if os.path.isfile(os.path.join(bundled, "devicemgmt.wsdl")):
            root = bundled
        else:
            import onvif

            root = os.path.join(os.path.dirname(os.path.dirname(onvif.__file__)), "wsdl")
    if not os.path.isfile(os.path.join(root, "devicemgmt.wsdl")):
        print(
            "Missing ONVIF WSDL files (e.g. devicemgmt.wsdl). The onvif-zeep pip wheel "
            "often does not install them under site-packages/wsdl.\n"
            "Fix: keep the `wsdl` folder next to this script (see project CODE/wsdl), or pass:\n"
            "  --wsdl-dir \"C:\\path\\to\\python-onvif-zeep\\wsdl\"",
            file=sys.stderr,
        )
        sys.exit(1)
    return root


def resolve_password(cli_password: str) -> str:
    env_pw = os.environ.get("CAMERA_PASSWORD", "").strip()
    if env_pw:
        return env_pw
    pw = cli_password.strip()
    if pw:
        return pw
    if sys.stdin.isatty():
        return getpass.getpass("Camera password: ")
    print(
        "No password: use -p, set CAMERA_PASSWORD, or run from a terminal for a prompt.",
        file=sys.stderr,
    )
    sys.exit(1)


def connect(
    host: str,
    port: int,
    user: str,
    password: str,
    timeout_sec: float,
    wsdl_dir: str,
) -> ONVIFCamera:
    transport = Transport(operation_timeout=timeout_sec)
    return ONVIFCamera(host, port, user, password, transport=transport, wsdl_dir=wsdl_dir)


def get_profiles(cam: ONVIFCamera) -> list[Any]:
    return list(cam.create_media_service().GetProfiles())


def profile_token(cam: ONVIFCamera, index: int) -> str:
    profiles = get_profiles(cam)
    if not profiles:
        sys.exit("Camera returned no media profiles.")
    if index < 0 or index >= len(profiles):
        sys.exit(f"Profile --profile {index} invalid; use 0..{len(profiles) - 1}.")
    return profiles[index].token


def cmd_test(cam: ONVIFCamera) -> None:
    dev = cam.create_devicemgmt_service()
    info = dev.GetDeviceInformation()
    profiles = get_profiles(cam)
    media = cam.create_media_service()
    token = profiles[0].token
    setup = media.create_type("GetStreamUri")
    setup.ProfileToken = token
    setup.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
    rtsp = media.GetStreamUri(setup).Uri

    print("ONVIF connection OK.")
    print(f"  {info.Manufacturer} {info.Model} (firmware {info.FirmwareVersion})")
    print(f"  Profiles: {len(profiles)} (using profile 0 for RTSP below)")
    print(f"  RTSP: {rtsp}")


def cmd_info(cam: ONVIFCamera) -> None:
    dev = cam.create_devicemgmt_service()
    info = dev.GetDeviceInformation()
    print(f"{info.Manufacturer} {info.Model}")
    print(f"Firmware: {info.FirmwareVersion}")
    print(f"Serial:   {info.SerialNumber}")


def cmd_profiles(cam: ONVIFCamera) -> None:
    for i, p in enumerate(get_profiles(cam)):
        name = getattr(p, "Name", "") or ""
        print(f"{i}: {p.token}  {name}".rstrip())


def cmd_rtsp(cam: ONVIFCamera, profile_index: int) -> None:
    media = cam.create_media_service()
    token = profile_token(cam, profile_index)
    setup = media.create_type("GetStreamUri")
    setup.ProfileToken = token
    setup.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
    print(media.GetStreamUri(setup).Uri)


def cmd_snapshot(cam: ONVIFCamera, profile_index: int) -> None:
    media = cam.create_media_service()
    token = profile_token(cam, profile_index)
    print(media.GetSnapshotUri({"ProfileToken": token}).Uri)


def ptz_service(cam: ONVIFCamera):
    try:
        return cam.create_ptz_service()
    except Exception as e:
        sys.exit(f"No PTZ service (fixed camera or ONVIF PTZ disabled): {e}")


def cmd_ptz(cam: ONVIFCamera, profile_index: int, pan: float, tilt: float, zoom: float, seconds: float) -> None:
    ptz = ptz_service(cam)
    media = cam.create_media_service()
    token = profile_token(cam, profile_index)
    profile = next(p for p in media.GetProfiles() if p.token == token)
    if profile.PTZConfiguration is None:
        sys.exit("This profile has no PTZ. Try another --profile (often 0 is the main stream).")

    move = ptz.create_type("ContinuousMove")
    move.ProfileToken = token
    move.Velocity = {"PanTilt": {"x": pan, "y": tilt}, "Zoom": {"x": zoom}}
    ptz.ContinuousMove(move)
    time.sleep(max(0.0, seconds))
    stop = ptz.create_type("Stop")
    stop.ProfileToken = token
    stop.PanTilt = True
    stop.Zoom = True
    ptz.Stop(stop)
    print("PTZ move finished.")


def cmd_stop(cam: ONVIFCamera, profile_index: int) -> None:
    ptz = ptz_service(cam)
    token = profile_token(cam, profile_index)
    stop = ptz.create_type("Stop")
    stop.ProfileToken = token
    stop.PanTilt = True
    stop.Zoom = True
    ptz.Stop(stop)
    print("PTZ stop sent.")


def cmd_home(cam: ONVIFCamera, profile_index: int) -> None:
    ptz = ptz_service(cam)
    token = profile_token(cam, profile_index)
    req = ptz.create_type("GotoHomePosition")
    req.ProfileToken = token
    req.Speed = None
    ptz.GotoHomePosition(req)
    print("Home position command sent.")


def main(argv: list[str] | None = None) -> int:
    actions = ["test", "info", "profiles", "rtsp", "snapshot", "ptz", "stop", "home"]
    parser = argparse.ArgumentParser(
        description=f"ONVIF control for IP camera (default host {DEFAULT_HOST}).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="test",
        choices=actions,
        help=f"What to run. Default: test (= quick connectivity check). Choices: {', '.join(actions)}.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Camera IP or hostname")
    parser.add_argument("-P", "--port", type=int, default=DEFAULT_PORT, help="ONVIF HTTP port (try 8899 if 80 fails)")
    parser.add_argument("-u", "--user", default=DEFAULT_USER, help="Login user")
    parser.add_argument(
        "-p",
        "--password",
        default=DEFAULT_PASSWORD,
        help="Login password (CAMERA_PASSWORD env overrides this default)",
    )
    parser.add_argument("--profile", type=int, default=0, help="Media profile index for rtsp/snapshot/ptz")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SEC,
        help="Per-request timeout in seconds",
    )
    parser.add_argument(
        "--wsdl-dir",
        default=None,
        help="Folder containing devicemgmt.wsdl (default: wsdl next to this script, else pip layout)",
    )
    parser.add_argument("--pan", type=float, default=0.0, help="[ptz] -1..1")
    parser.add_argument("--tilt", type=float, default=0.0, help="[ptz] -1..1")
    parser.add_argument("--zoom", type=float, default=0.0, help="[ptz] -1..1")
    parser.add_argument("--seconds", type=float, default=1.0, help="[ptz] move duration before stop")

    args = parser.parse_args(argv)
    password = resolve_password(args.password)
    wsdl_dir = resolve_wsdl_dir(args.wsdl_dir)

    try:
        cam = connect(args.host, args.port, args.user, password, args.timeout, wsdl_dir)
    except OSError as e:
        print(f"Network error (wrong IP/port or camera offline?): {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Could not connect or authenticate: {e}", file=sys.stderr)
        return 1

    try:
        if args.action == "test":
            cmd_test(cam)
        elif args.action == "info":
            cmd_info(cam)
        elif args.action == "profiles":
            cmd_profiles(cam)
        elif args.action == "rtsp":
            cmd_rtsp(cam, args.profile)
        elif args.action == "snapshot":
            cmd_snapshot(cam, args.profile)
        elif args.action == "ptz":
            cmd_ptz(cam, args.profile, args.pan, args.tilt, args.zoom, args.seconds)
        elif args.action == "stop":
            cmd_stop(cam, args.profile)
        elif args.action == "home":
            cmd_home(cam, args.profile)
    except Exception as e:
        print(f"Command failed: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
