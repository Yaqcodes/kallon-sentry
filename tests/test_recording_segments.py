"""Registry recording segment tests (SQLite provider)."""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from registry.interface import RecordingSegment
from registry import Customer, Tower, get_registry  # noqa: E402
from registry.identity import device_id, new_claim_code, new_enrollment_token, slug_of  # noqa: E402


def _seed(reg):
    slug = "rec"
    cust = Customer(
        customer_id=f"cust_{slug}",
        display_name="Rec Test",
        vpn_subnet="10.60.0.0/24",
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
    return cust.customer_id, dev


def test_recording_segment_crud() -> None:
    db = os.path.join(tempfile.gettempdir(), f"kallon_rec_test_{uuid.uuid4().hex}.db")
    os.environ["KALLON_REGISTRY"] = "sqlite"
    os.environ["KALLON_SQLITE_PATH"] = db
    reg = get_registry()
    reg.init_schema()
    customer_id, device_id_val = _seed(reg)
    started = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    seg = RecordingSegment(
        segment_id=str(uuid.uuid4()),
        customer_id=customer_id,
        device_id=device_id_val,
        camera=1,
        filename="2026-07-15_12-00-00-000000.mp4",
        s3_bucket="kallon-recordings",
        s3_key=f"{device_id_val}/cam1/2026-07-15_12-00-00-000000.mp4",
        size_bytes=123456,
        sha256_hex="abc",
        started_at=started,
        uploaded_at=datetime.now(timezone.utc),
        duration_sec=900,
    )
    saved = reg.upsert_recording_segment(seg)
    assert saved.segment_id == seg.segment_id
    rows = reg.list_recording_segments(customer_id=customer_id, device_id=device_id_val)
    assert len(rows) == 1
    assert rows[0].filename.endswith(".mp4")
    deleted = reg.delete_recording_segment(seg.segment_id)
    assert deleted.segment_id == seg.segment_id
    reg.close()
    os.remove(db)
    print("test_recording_segment_crud OK")


if __name__ == "__main__":
    test_recording_segment_crud()
