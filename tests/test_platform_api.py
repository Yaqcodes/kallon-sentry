"""Integration test for the Platform API (fleet + tower proxy).

Run: python tests/test_platform_api.py
Requires: fastapi, httpx (TestClient).

Uses the SQLite registry and a mock tower-gateway HTTP server on loopback so
the proxy path (control plane -> tower gateway) is exercised without hardware.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Configure providers BEFORE importing the app.
_db = os.path.join(tempfile.gettempdir(), "kallon_platform_test.db")
if os.path.exists(_db):
    os.remove(_db)
os.environ["KALLON_REGISTRY"] = "sqlite"
os.environ["KALLON_SQLITE_PATH"] = _db
os.environ["KALLON_PEER_BACKEND"] = "noop"
os.environ.pop("ENROLLMENT_HMAC_KEY", None)
os.environ.pop("KALLON_PLATFORM_API_KEY", None)

MOCK_GATEWAY_PORT = 18766
os.environ["KALLON_TOWER_GATEWAY_PORT"] = str(MOCK_GATEWAY_PORT)

from registry import Customer, Tower, get_registry  # noqa: E402
from registry.identity import device_id, new_claim_code, new_enrollment_token  # noqa: E402

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"J" * 64 + b"\xff\xd9"


class MockGateway(BaseHTTPRequestHandler):
    """Mimics infra/tower-dashboard/gateway.py responses."""

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._json(200, {"available": True, "temperature_c": 45.0})
        elif path == "/api/streams":
            self._json(200, {"available": True, "paths": [{"name": "cam1", "ready": True}]})
        elif path == "/api/ptz/status":
            self._json(200, {"ok": True, "result": {"pan": 0.1, "tilt": -0.2, "zoom": 0.0}})
        elif path == "/api/snapshot/cam1":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(FAKE_JPEG)))
            self.end_headers()
            self.wfile.write(FAKE_JPEG)
        elif path == "/api/snapshot/cam9":
            self._json(404, {"error": {"code": "not_found", "message": "camera 9 out of range"}})
        else:
            self._json(404, {"error": {"code": "not_found", "message": path}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/ptz/move":
            self._json(200, {"ok": True, "result": {"ok": True, "round_trip_ms": 42.0, "echo": body}})
        elif path == "/api/ptz/stop":
            self._json(200, {"ok": True, "result": {}})
        else:
            self._json(404, {"error": {"code": "not_found", "message": path}})

    def log_message(self, *args) -> None:
        pass


def seed() -> dict:
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
    tokens = {}
    for serial in (1, 2):
        tok = new_enrollment_token()
        did = device_id("lab", serial)
        reg.register_tower(Tower(
            device_id=did, customer_id="cust_lab", claim_code=new_claim_code(),
            enrollment_token_hash=hashlib.sha256(tok.encode()).hexdigest(),
        ))
        tokens[did] = tok
    # Tower 1 "enrolls" with a loopback VPN IP so the proxy hits our mock
    # gateway. Tower 2 stays unenrolled (no vpn_ip) for the 409 path.
    reg.mark_tower_enrolled(device_id("lab", 1), wg_public_key="T1" + "A" * 41 + "=", vpn_ip="127.0.0.1")
    reg.close()
    return tokens


def main() -> int:
    seed()
    gateway = ThreadingHTTPServer(("127.0.0.1", MOCK_GATEWAY_PORT), MockGateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()

    from fastapi.testclient import TestClient
    from app.main import app  # type: ignore

    client = TestClient(app)
    failures = 0

    def check(cond, label):
        nonlocal failures
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            failures += 1

    d1 = device_id("lab", 1)
    d2 = device_id("lab", 2)

    # ── fleet ────────────────────────────────────────────────────────────────
    r = client.get("/v1/customers")
    check(r.status_code == 200 and r.json()["customers"][0]["customer_id"] == "cust_lab",
          "list customers")

    r = client.get("/v1/customers/cust_lab")
    check(r.json()["vpn_subnet"] == "10.50.0.0/24", "get customer")

    r = client.get("/v1/customers/cust_nope")
    check(r.status_code == 404 and r.json()["error"]["code"] == "not_found",
          "unknown customer 404 envelope")

    r = client.get("/v1/customers/cust_lab/towers")
    towers = r.json()["towers"]
    check(len(towers) == 2, f"list customer towers ({len(towers)})")
    t1 = next(t for t in towers if t["device_id"] == d1)
    check(t1["rtsp_base"] == "rtsp://127.0.0.1:8554", "rtsp_base derived")
    check("claim_code" not in t1 and "enrollment_token_hash" not in t1,
          "secrets excluded from tower payload")

    r = client.get(f"/v1/towers/{d1}")
    check(r.status_code == 200 and r.json()["vpn_ip"] == "127.0.0.1", "get tower")

    r = client.post("/v1/towers", json={"customer_id": "cust_lab", "serial": 3})
    check(r.status_code == 201, f"register tower ({r.status_code})")
    body = r.json()
    check(body["device_id"] == device_id("lab", 3), "registered device_id")
    check(body["enrollment_token"].startswith("enr_"), "one-time token returned")

    r = client.post("/v1/towers", json={"customer_id": "cust_lab", "serial": 3})
    check(r.status_code == 409, f"duplicate registration 409 ({r.status_code})")

    # ── tower proxy: enrolled tower vs mock gateway ─────────────────────────
    r = client.get(f"/v1/towers/{d1}/status")
    check(r.status_code == 200 and r.json()["temperature_c"] == 45.0, "proxy status")

    r = client.get(f"/v1/towers/{d1}/streams")
    check(r.json()["paths"][0]["ready"] is True, "proxy streams")

    r = client.post(f"/v1/towers/{d1}/ptz/move",
                    json={"camera": 1, "mode": "absolute", "pan": 0.5, "tilt": -0.2})
    check(r.status_code == 200 and r.json()["result"]["round_trip_ms"] == 42.0, "proxy ptz move")

    r = client.post(f"/v1/towers/{d1}/ptz/move", json={"mode": "sideways"})
    check(r.status_code == 422 and r.json()["error"]["code"] == "invalid_request",
          "bad ptz mode 422 (validated before proxy)")

    r = client.post(f"/v1/towers/{d1}/ptz/stop", json={})
    check(r.status_code == 200, "proxy ptz stop")

    r = client.get(f"/v1/towers/{d1}/ptz/status?camera=1")
    check(r.json()["result"]["pan"] == 0.1, "proxy ptz status")

    r = client.get(f"/v1/towers/{d1}/snapshot/cam1")
    check(r.status_code == 200 and r.headers["content-type"].startswith("image/jpeg")
          and r.content == FAKE_JPEG, "proxy snapshot jpeg")

    r = client.get(f"/v1/towers/{d1}/snapshot/cam9")
    check(r.status_code == 404, "snapshot bad camera passthrough 404")

    # ── tower proxy: failure contracts ──────────────────────────────────────
    r = client.get(f"/v1/towers/{d2}/status")
    check(r.status_code == 409 and r.json()["error"]["code"] == "tower_not_enrolled",
          "unenrolled tower 409")

    r = client.get("/v1/towers/kln_lab_000099/status")
    check(r.status_code == 404, "unknown tower 404")

    # Offline: point tower 1's gateway port somewhere closed.
    gateway.shutdown()
    r = client.get(f"/v1/towers/{d1}/status")
    check(r.status_code == 503 and r.json()["error"]["code"] == "tower_offline",
          "offline tower 503 tower_offline")
    check(r.json()["error"]["device_id"] == d1, "offline error includes device_id")

    print(f"\n{'ALL OK' if failures == 0 else str(failures) + ' FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(ROOT, "infra", "enrollment-api"))
    raise SystemExit(main())
