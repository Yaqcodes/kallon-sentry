#!/usr/bin/env python3
"""Seed Railway Postgres with the lab customer, tower, and hub endpoint.

Usage (from repo root):

  $env:DATABASE_URL = "postgresql://USER:PASS@reseau.proxy.rlwy.net:16787/railway?sslmode=require"
  $env:KALLON_REGISTRY = "postgres"
  python scripts/railway-seed-lab.py

Idempotent: creates customer/tower if missing, always refreshes hub + enrolls tower.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from registry import Conflict, NotFound, get_registry  # noqa: E402
from registry.identity import (  # noqa: E402
    customer_id as make_customer_id,
    device_id as make_device_id,
    new_claim_code,
    new_enrollment_token,
)
from registry.interface import Customer, Tower  # noqa: E402

SLUG = "lab"
SERIAL = 1
DISPLAY = "Kallon Lab"
SUBNET = "10.50.0.0/24"
HUB_ENDPOINT = "18.116.127.232:51820"
HUB_PUBKEY = "mXuS1kDIK49t6TFcDRJ9AqlKdb+jDxkilNcaM+oWdnw="
HUB_HOST_ID = "kallon-gateway-lab-2g"
TOWER_VPN_IP = "10.50.0.2"
TOWER_WG_PUB = "Z/GdJ5DflhRjxnIpFvMrrdV1RBJ9tiu/IqdfnLZhoV4="


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL is unset", file=sys.stderr)
        print(
            "Set it to the Railway public URL, e.g.\n"
            "  postgresql://postgres:PASS@reseau.proxy.rlwy.net:16787/railway?sslmode=require",
            file=sys.stderr,
        )
        return 2
    os.environ.setdefault("KALLON_REGISTRY", "postgres")

    cid = make_customer_id(SLUG)
    did = make_device_id(SLUG, SERIAL)

    reg = get_registry("postgres")
    try:
        print("init-schema…")
        reg.init_schema()

        try:
            reg.get_customer(cid)
            print(f"customer exists: {cid}")
        except NotFound:
            reg.create_customer(
                Customer(
                    customer_id=cid,
                    display_name=DISPLAY,
                    vpn_subnet=SUBNET,
                    hub_provider="lightsail",
                    status="pending_hub",
                )
            )
            print(f"created customer: {cid}")

        cust = reg.update_customer_hub(
            cid,
            gateway_endpoint=HUB_ENDPOINT,
            gateway_public_key=HUB_PUBKEY,
            hub_provider="lightsail",
            hub_host_id=HUB_HOST_ID,
            status="active",
            hub_alert_url="http://10.50.0.1:8080/alerts",
        )
        print(f"hub set: {cust.gateway_endpoint} status={cust.status}")

        try:
            tower = reg.get_tower(did)
            print(f"tower exists: {did} status={tower.status}")
        except NotFound:
            token = new_enrollment_token()
            claim = new_claim_code()
            tower = Tower(
                device_id=did,
                customer_id=cid,
                claim_code=claim,
                enrollment_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                status="manufactured",
            )
            try:
                reg.register_tower(tower)
                print(f"created tower: {did}")
                print(f"  enrollment_token (once): {token}")
                print(f"  claim_code: {claim}")
            except Conflict:
                print(f"tower conflict, reloading: {did}")

        tower = reg.mark_tower_enrolled(
            did, wg_public_key=TOWER_WG_PUB, vpn_ip=TOWER_VPN_IP
        )
        tower = reg.set_tower_status(did, "active")
        print(f"tower ready: {did} vpn={tower.vpn_ip} status={tower.status}")

        print("OK")
        for c in reg.list_customers():
            print(f"  customer {c.customer_id} hub={c.gateway_endpoint} status={c.status}")
        for t in reg.list_towers(cid):
            print(f"  tower {t.device_id} vpn={t.vpn_ip} status={t.status}")
        return 0
    finally:
        reg.close()


if __name__ == "__main__":
    raise SystemExit(main())
