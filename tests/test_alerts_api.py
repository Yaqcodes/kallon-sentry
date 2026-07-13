"""Integration test for dashboard alert ingest + query + SSE.

Run: python tests/test_alerts_api.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_db = os.path.join(tempfile.gettempdir(), "kallon_alerts_test.db")
if os.path.exists(_db):
    os.remove(_db)
os.environ["KALLON_REGISTRY"] = "sqlite"
os.environ["KALLON_SQLITE_PATH"] = _db
os.environ["KALLON_PEER_BACKEND"] = "noop"
os.environ.pop("ENROLLMENT_HMAC_KEY", None)
os.environ.pop("KALLON_PLATFORM_API_KEY", None)
os.environ.pop("KALLON_ALERT_INGEST_TOKEN", None)

from registry import Customer, Tower, get_registry  # noqa: E402
from registry.identity import device_id, new_claim_code, new_enrollment_token  # noqa: E402

D1 = device_id("lab", 1)
D2 = device_id("lab", 2)


def seed() -> None:
    reg = get_registry()
    reg.create_customer(Customer(
        customer_id="cust_lab", display_name="Kallon Lab",
        vpn_subnet="10.50.0.0/24", hub_provider="manual",
    ))
    reg.update_customer_hub(
        "cust_lab",
        gateway_endpoint="198.51.100.9:51820",
        gateway_public_key="HUBPUBKEY==",
        hub_alert_url="http://10.50.0.1:8080/alerts",
        status="active",
    )
    for serial in (1, 2):
        tok = new_enrollment_token()
        did = device_id("lab", serial)
        reg.register_tower(Tower(
            device_id=did, customer_id="cust_lab", claim_code=new_claim_code(),
            enrollment_token_hash=hashlib.sha256(tok.encode()).hexdigest(),
        ))
    reg.mark_tower_enrolled(D1, wg_public_key="T1" + "A" * 41 + "=", vpn_ip="10.50.0.2")
    reg.close()


def sample_alert(device: str, *, nonce: str = "n1") -> dict:
    return {
        "device_id": device,
        "timestamp_utc": "2025-06-05T14:30:00Z",
        "nonce": nonce,
        "alert_type": "tamper_impact",
        "severity": "critical",
        "details": {"axis_mg": 312},
    }


def main() -> int:
    seed()
    sys.path.insert(0, os.path.join(ROOT, "infra", "enrollment-api"))
    from fastapi.testclient import TestClient
    from app.main import app  # type: ignore

    client = TestClient(app)
    failures = 0

    def check(cond, label):
        nonlocal failures
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            failures += 1

    r = client.post("/v1/alerts/ingest", json=sample_alert(D1))
    check(r.status_code == 201 and r.json()["status"] == "accepted", "ingest alert 201")
    check(r.json()["alert"]["customer_id"] == "cust_lab", "ingest resolves customer_id")

    r = client.post("/v1/alerts/ingest", json=sample_alert(D1))
    check(r.status_code == 200 and r.json()["status"] == "duplicate", "ingest dedup")

    r = client.post("/v1/alerts/ingest", json=sample_alert(D1, nonce="n2"))
    check(r.status_code == 201, "ingest second alert")

    r = client.post("/v1/alerts/ingest", json={"alert_type": "orphan"})
    check(r.status_code == 422, "ingest missing device_id 422")

    r = client.post("/v1/alerts/ingest", json=sample_alert("kln_nope_000001", nonce="n3"))
    check(r.status_code == 404, "ingest unknown device 404")

    r = client.get("/v1/alerts")
    check(len(r.json()["alerts"]) == 2, f"list alerts ({len(r.json()['alerts'])})")

    r = client.get("/v1/customers/cust_lab/alerts")
    check(len(r.json()["alerts"]) == 2, "list customer alerts")

    r = client.get("/v1/customers/cust_nope/alerts")
    check(r.status_code == 404, "unknown customer alerts 404")

    # OpenAPI should expose fleet + proxy + alerts, not just enrollment.
    spec = client.get("/openapi.json").json()
    paths = set(spec.get("paths", {}))
    check("/v1/customers" in paths, "openapi includes fleet")
    check("/v1/towers/{device_id}/ptz/move" in paths, "openapi includes tower proxy")
    check("/v1/alerts/ingest" in paths, "openapi includes alerts ingest")
    check("/v1/events" in paths, "openapi includes alerts sse")
    tags = {t["name"] for t in spec.get("tags", [])}
    check("Fleet" in tags and "Alerts" in tags, "openapi tag groups present")

    print(f"\n{'ALL OK' if failures == 0 else str(failures) + ' FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
