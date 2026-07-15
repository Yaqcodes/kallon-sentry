#!/usr/bin/env python3
"""Sync Lightsail public ports for an existing hub (SSH + WG + tower-proxy + HLS).

Use when a hub was created before TCP 8767/8768 was automated, or after a console
edit wiped ports. put_instance_public_ports replaces the entire set.

  python infra/hub-provisioner/sync_lightsail_ports.py kallon-hub-cust_lab --region us-east-2

Env:
  KALLON_HUB_PROXY_PORT  default 8767
  KALLON_HUB_HLS_PORT    default 8768
  AWS credentials via normal boto3 chain
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    try:
        import boto3
    except ImportError:
        print("boto3 required", file=sys.stderr)
        return 1

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("instance_name", help="Lightsail instance name (e.g. kallon-hub-cust_lab)")
    p.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    p.add_argument(
        "--proxy-port",
        type=int,
        default=int(os.environ.get("KALLON_HUB_PROXY_PORT", "8767") or "8767"),
    )
    p.add_argument(
        "--hls-port",
        type=int,
        default=int(os.environ.get("KALLON_HUB_HLS_PORT", "8768") or "8768"),
    )
    args = p.parse_args()

    port_infos = [
        {"fromPort": 22, "toPort": 22, "protocol": "tcp"},
        {"fromPort": 51820, "toPort": 51820, "protocol": "udp"},
        {"fromPort": args.proxy_port, "toPort": args.proxy_port, "protocol": "tcp"},
        {"fromPort": args.hls_port, "toPort": args.hls_port, "protocol": "tcp"},
    ]
    ls = boto3.client("lightsail", region_name=args.region)
    ls.put_instance_public_ports(instanceName=args.instance_name, portInfos=port_infos)
    print(f"OK: {args.instance_name} ({args.region}) ports → {port_infos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
