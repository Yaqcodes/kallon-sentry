"""Integration test for the enrollment API (SQLite registry + noop peer adder).

Run: python tests/test_enrollment_api.py
Requires: fastapi, httpx (TestClient).
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Configure providers BEFORE importing the app.
_db = os.path.join(tempfile.gettempdir(), "kallon_enroll_test.db")
if os.path.exists(_db):
    os.remove(_db)
os.environ["KALLON_REGISTRY"] = "sqlite"
os.environ["KALLON_SQLITE_PATH"] = _db
os.environ["KALLON_PEER_BACKEND"] = "noop"
os.environ.pop("ENROLLMENT_HMAC_KEY", None)

from registry import Customer, Tower, get_registry  # noqa: E402
from registry.identity import (  # noqa: E402
    customer_id,
    device_id,
    new_claim_code,
    new_enrollment_token,
)


def seed():
    reg = get_registry()
    reg.create_customer(Customer(
        customer_id=customer_id("lab"),
        display_name="Kallon Lab",
        vpn_subnet="10.50.0.0/24",
        hub_provider="lightsail",
    ))
    reg.update_customer_hub(
        "cust_lab",
        gateway_endpoint="198.51.100.9:51820",
        gateway_public_key="HUBPUBKEY==",
        hub_alert_url="http://10.50.0.1:8080/alerts",
        status="active",
    )
    tokens = {}
    for serial in (1, 2):
        tok = new_enrollment_token()
        did = device_id("lab", serial)
        reg.register_tower(Tower(
            device_id=did,
            customer_id="cust_lab",
            claim_code=new_claim_code(),
            enrollment_token_hash=hashlib.sha256(tok.encode()).hexdigest(),
        ))
        tokens[did] = tok
    reg.close()
    return tokens


def main() -> int:
    tokens = seed()
    from fastapi.testclient import TestClient
    from app.main import app  # type: ignore

    # Make `app` importable as a top-level package path.
    client = TestClient(app)
    failures = 0

    def check(cond, label):
        nonlocal failures
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            failures += 1

    check(client.get("/healthz").json()["status"] == "ok", "healthz")

    # Enroll tower 1.
    d1 = device_id("lab", 1)
    r = client.post("/v1/enroll", json={
        "device_id": d1, "wg_public_key": "TOWER1PUB==", "enrollment_token": tokens[d1],
    })
    check(r.status_code == 200, f"enroll t1 status ({r.status_code})")
    body = r.json()
    check(body["vpn_ip"] == "10.50.0.2", f"t1 vpn_ip ({body.get('vpn_ip')})")
    check(body["gateway_public_key"] == "HUBPUBKEY==", "t1 gateway pubkey")
    confirm1 = body["confirm_token"]

    # Idempotent re-enroll with same key returns same IP (and a fresh confirm
    # token, which supersedes the previous one — use the latest for confirm).
    r = client.post("/v1/enroll", json={
        "device_id": d1, "wg_public_key": "TOWER1PUB==", "enrollment_token": tokens[d1],
    })
    check(r.json()["vpn_ip"] == "10.50.0.2", "t1 re-enroll idempotent IP")
    confirm1 = r.json()["confirm_token"]

    # Bad token rejected.
    r = client.post("/v1/enroll", json={
        "device_id": d1, "wg_public_key": "x" * 8, "enrollment_token": "enr_wrong",
    })
    check(r.status_code == 401, f"bad token rejected ({r.status_code})")

    # Enroll tower 2 → next IP.
    d2 = device_id("lab", 2)
    r = client.post("/v1/enroll", json={
        "device_id": d2, "wg_public_key": "TOWER2PUB==", "enrollment_token": tokens[d2],
    })
    check(r.json()["vpn_ip"] == "10.50.0.3", f"t2 vpn_ip ({r.json().get('vpn_ip')})")

    # Confirm tower 1.
    r = client.post("/v1/enroll/confirm", json={
        "device_id": d1, "confirm_token": confirm1, "handshake_ok": True,
    })
    check(r.status_code == 200 and r.json()["status"] == "active", "t1 confirm active")

    # Wrong confirm token rejected.
    r = client.post("/v1/enroll/confirm", json={
        "device_id": d1, "confirm_token": "cnf_bad", "handshake_ok": True,
    })
    check(r.status_code == 401, "bad confirm token rejected")

    # Verify final registry state.
    reg = get_registry()
    t1 = reg.get_tower(d1)
    check(t1.status == "active" and t1.vpn_ip == "10.50.0.2", "t1 registry active")
    reg.close()

    print(f"\n{'ALL OK' if failures == 0 else str(failures) + ' FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    # Allow `from app.main import app` by adding the API dir to sys.path.
    sys.path.insert(0, os.path.join(ROOT, "infra", "enrollment-api"))
    raise SystemExit(main())
