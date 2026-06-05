#!/usr/bin/env python3
"""kallon-ptz-benchmark — measure PTZ command latency against the PTZ daemon.

Runs the Phase 4 / brief SLA check: send N PTZ commands over the daemon's
newline-delimited JSON TCP socket and report p50/p95/p99/max round-trip latency.
Use this on the pilot Jetson to either confirm the sub-100 ms target or formally
re-baseline against the ONVIF/Dahua ceiling.

Examples:
  # 1,000 move_absolute commands (the SLA run)
  python3 scripts/kallon-ptz-benchmark.py --count 1000

  # IPC-only baseline (no camera movement) to isolate daemon overhead
  python3 scripts/kallon-ptz-benchmark.py --count 1000 --method ping
"""
from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import time


def send_one(sock_file, sock, rid: int, method: str, params: dict) -> tuple[bool, float, str]:
    req = json.dumps({"id": rid, "method": method, "params": params}, separators=(",", ":")) + "\n"
    t0 = time.perf_counter()
    sock.sendall(req.encode())
    line = sock_file.readline()
    rtt_ms = (time.perf_counter() - t0) * 1000.0
    if not line:
        return False, rtt_ms, "no response (connection closed)"
    try:
        resp = json.loads(line)
    except json.JSONDecodeError as e:
        return False, rtt_ms, f"bad json: {e}"
    return bool(resp.get("ok")), rtt_ms, "" if resp.get("ok") else str(resp.get("error"))


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--method", default="move_absolute",
                    choices=["move_absolute", "ping", "get_position"])
    ap.add_argument("--sla-ms", type=float, default=100.0, help="p95 target to flag against")
    ap.add_argument("--pan", type=float, default=0.3)
    ap.add_argument("--tilt", type=float, default=0.1)
    args = ap.parse_args(argv)

    try:
        sock = socket.create_connection((args.host, args.port), timeout=10)
    except OSError as e:
        print(f"ERROR: cannot connect to PTZ daemon at {args.host}:{args.port}: {e}",
              file=sys.stderr)
        return 1
    sock_file = sock.makefile("r")

    latencies: list[float] = []
    errors = 0
    print(f"running {args.count} x {args.method} against {args.host}:{args.port} ...")
    for i in range(args.count):
        if args.method == "move_absolute":
            # Alternate position each call so the camera actually moves.
            sign = 1 if i % 2 == 0 else -1
            params = {"pan": args.pan * sign, "tilt": args.tilt * sign}
        else:
            params = {}
        ok, rtt, err = send_one(sock_file, sock, i + 1, args.method, params)
        latencies.append(rtt)
        if not ok:
            errors += 1
            if errors <= 5:
                print(f"  cmd {i + 1} failed: {err}", file=sys.stderr)

    sock_file.close()
    sock.close()

    ok_count = args.count - errors
    p95 = pct(latencies, 95)
    print("\n── PTZ latency ──────────────────────────────")
    print(f"commands     : {args.count}  (ok={ok_count}, errors={errors})")
    print(f"mean         : {statistics.fmean(latencies):8.2f} ms")
    print(f"p50          : {pct(latencies, 50):8.2f} ms")
    print(f"p95          : {p95:8.2f} ms")
    print(f"p99          : {pct(latencies, 99):8.2f} ms")
    print(f"max          : {max(latencies):8.2f} ms")
    print(f"SLA target   : p95 < {args.sla_ms:.0f} ms")
    if p95 < args.sla_ms:
        print("RESULT       : PASS (within SLA)")
        return 0
    print("RESULT       : OVER SLA — document the ONVIF ceiling or re-baseline the target")
    return 0  # over-SLA is a finding to record, not a script failure


if __name__ == "__main__":
    raise SystemExit(main())
