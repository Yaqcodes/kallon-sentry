"""Platform recordings API tests (ingest + tenant-scoped list)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "infra", "enrollment-api"))

_db = os.path.join(tempfile.gettempdir(), f"kallon_rec_api_{uuid.uuid4().hex}.db")
os.environ["KALLON_REGISTRY"] = "sqlite"
os.environ["KALLON_SQLITE_PATH"] = _db
os.environ["KALLON_PEER_BACKEND"] = "noop"
os.environ["KALLON_RECORDING_INGEST_TOKEN"] = "test-ingest-token"
os.environ.pop("KALLON_PLATFORM_API_KEY", None)

from registry import Customer, Tower, get_registry  # noqa: E402
from registry.identity import device_id, new_claim_code, new_enrollment_token  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _seed() -> tuple[str, str]:
    reg = get_registry()
    slug = "recapi"
    cust = Customer(
        customer_id=f"cust_{slug}",
        display_name="Rec API",
        vpn_subnet="10.61.0.0/24",
    )
    reg.create_customer(cust)
    dev = device_id(slug, 1)
    reg.register_tower(
        Tower(
            device_id=dev,
            customer_id=cust.customer_id,
            claim_code=new_claim_code(),
            enrollment_token_hash=new_enrollment_token()[1],
        )
    )
    reg.close()
    return cust.customer_id, dev


def test_recordings_ingest_and_list() -> None:
    customer_id, device_id_val = _seed()
    client = TestClient(app)
    body = {
        "device_id": device_id_val,
        "camera": 2,
        "filename": "2026-07-15_12-15-00-000000.mp4",
        "s3_bucket": "kallon-recordings",
        "s3_key": f"{device_id_val}/cam2/2026-07-15_12-15-00-000000.mp4",
        "size_bytes": 999,
        "sha256_hex": "deadbeef",
        "started_at": "2026-07-15T12:15:00Z",
        "duration_sec": 900,
    }
    resp = client.post(
        "/v1/recordings/ingest",
        content=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            "X-Kallon-Ingest-Token": "test-ingest-token",
        },
    )
    assert resp.status_code == 201, resp.text
    segment_id = resp.json()["segment"]["segment_id"]

    listed = client.get(f"/v1/customers/{customer_id}/recordings?device_id={device_id_val}")
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["retention_days"] == 30
    assert len(data["segments"]) == 1
    assert data["segments"][0]["segment_id"] == segment_id

    other = client.get("/v1/customers/cust_other/recordings")
    assert other.status_code == 404

    print("test_recordings_ingest_and_list OK")


if __name__ == "__main__":
    test_recordings_ingest_and_list()
