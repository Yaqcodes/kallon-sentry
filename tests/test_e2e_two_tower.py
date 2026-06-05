"""End-to-end software harness: provision a hub (registry), enroll two towers,
and prove the hub wg0.conf is built entirely by automation (no hand editing).

What this covers without hardware:
  * registry: customer + subnet + two tower rows + monotonic IP allocation
  * enrollment API: two towers enroll → distinct /32s → confirm → active
  * peer persistence: the canonical wg_peers algorithm yields exactly two peers,
    is idempotent, and replaces (never duplicates) on key rotation

Hardware-gated (NOT covered here; see docs/customer-gateway.md):
  * live WireGuard handshake, RTSP pull over VPN, real SSH bring-up, UFW.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "infra", "enrollment-api"))
sys.path.insert(0, os.path.join(ROOT, "infra", "hub"))

_db = os.path.join(tempfile.gettempdir(), "kallon_e2e.db")
if os.path.exists(_db):
    os.remove(_db)
os.environ["KALLON_REGISTRY"] = "sqlite"
os.environ["KALLON_SQLITE_PATH"] = _db
os.environ["KALLON_PEER_BACKEND"] = "noop"
os.environ.pop("ENROLLMENT_HMAC_KEY", None)

from registry import Customer, Tower, get_registry  # noqa: E402
from registry.identity import customer_id, device_id, new_claim_code, new_enrollment_token  # noqa: E402
from wg_peers import add_or_replace_peer, count_peers  # type: ignore  # noqa: E402

FAIL = 0


def check(cond, label):
    global FAIL
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAIL += 1


def provision_hub():
    """Simulate kallon-hub-provision result: active customer hub in the registry."""
    reg = get_registry()
    reg.create_customer(Customer(
        customer_id=customer_id("lab"), display_name="Kallon Lab",
        vpn_subnet="10.50.0.0/24", hub_provider="manual",
    ))
    reg.update_customer_hub(
        "cust_lab", gateway_endpoint="203.0.113.42:51820",
        gateway_public_key="HUBPUB==", hub_alert_url="http://10.50.0.1:8080/alerts",
        hub_host_id="203.0.113.42", status="active",
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
    reg.close()
    return tokens


def main() -> int:
    tokens = provision_hub()
    from fastapi.testclient import TestClient
    from app.main import app  # type: ignore

    client = TestClient(app)

    pubkeys = {device_id("lab", 1): "TOWER1PUB==", device_id("lab", 2): "TOWER2PUB=="}
    results = {}
    for did, pub in pubkeys.items():
        r = client.post("/v1/enroll", json={
            "device_id": did, "wg_public_key": pub, "enrollment_token": tokens[did],
        })
        check(r.status_code == 200, f"enroll {did} ({r.status_code})")
        results[did] = r.json()

    ips = {did: results[did]["vpn_ip"] for did in pubkeys}
    check(ips[device_id("lab", 1)] == "10.50.0.2", f"t1 ip {ips[device_id('lab',1)]}")
    check(ips[device_id("lab", 2)] == "10.50.0.3", f"t2 ip {ips[device_id('lab',2)]}")
    check(len(set(ips.values())) == 2, "two distinct VPN IPs (no cross-traffic overlap)")

    # confirm both
    for did in pubkeys:
        r = client.post("/v1/enroll/confirm", json={
            "device_id": did, "confirm_token": results[did]["confirm_token"], "handshake_ok": True,
        })
        check(r.status_code == 200 and r.json()["status"] == "active", f"confirm {did} active")

    # ── hub wg0.conf assembled purely by automation ───────────────────────────
    conf = "[Interface]\nAddress = 10.50.0.1/24\nListenPort = 51820\nPrivateKey = HUBPRIV==\n"
    for did, pub in pubkeys.items():
        conf = add_or_replace_peer(conf, pub, ips[did], did)
    check(count_peers(conf) == 2, f"wg0.conf has 2 peers ({count_peers(conf)})")
    check("AllowedIPs = 10.50.0.2/32" in conf, "t1 peer AllowedIPs present")
    check("AllowedIPs = 10.50.0.3/32" in conf, "t2 peer AllowedIPs present")
    check("# kln_lab_000001" in conf and "# kln_lab_000002" in conf, "device-id peer comments")

    # idempotent: re-adding the same peer does not duplicate
    conf2 = add_or_replace_peer(conf, "TOWER1PUB==", "10.50.0.2/32", device_id("lab", 1))
    check(count_peers(conf2) == 2, "idempotent re-add keeps 2 peers")

    # rotation: same device, NEW key → old key block removed, still 2 peers
    conf3 = add_or_replace_peer(conf2, "TOWER1ROTATED==", "10.50.0.2/32", device_id("lab", 1))
    check(count_peers(conf3) == 2, "key rotation keeps 2 peers")
    check("TOWER1PUB==" not in conf3 and "TOWER1ROTATED==" in conf3, "old key replaced by rotated key")

    # interface block untouched
    check(conf3.startswith("[Interface]") and "ListenPort = 51820" in conf3, "interface block intact")

    print(f"\n{'ALL OK' if FAIL == 0 else str(FAIL) + ' FAILURES'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
