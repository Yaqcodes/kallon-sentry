"""CORS preflight for browser dashboards (Vercel → Platform API)."""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "infra", "enrollment-api"))

_db = os.path.join(tempfile.gettempdir(), "kallon_cors_test.db")
if os.path.exists(_db):
    os.remove(_db)
os.environ["KALLON_REGISTRY"] = "sqlite"
os.environ["KALLON_SQLITE_PATH"] = _db
os.environ["KALLON_PEER_BACKEND"] = "noop"
os.environ["KALLON_CORS_ORIGINS"] = "https://app.example.vercel.app"
os.environ.pop("ENROLLMENT_HMAC_KEY", None)


def main() -> int:
    from fastapi.testclient import TestClient
    from app.main import app  # type: ignore

    client = TestClient(app)
    origin = "https://app.example.vercel.app"
    res = client.options(
        "/v1/customers/cust_lab/towers",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-kallon-api-key",
        },
    )
    assert res.status_code == 200, res.text
    assert res.headers.get("access-control-allow-origin") == origin
    print("test_cors: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
