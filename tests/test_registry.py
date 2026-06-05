"""Unit tests for the registry using the in-memory SQLite provider.

Run:  python -m pytest tests/test_registry.py
  or: python tests/test_registry.py   (falls back to a tiny runner)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry import Conflict, Customer, NotFound, SubnetExhausted, Tower  # noqa: E402
from registry.identity import (  # noqa: E402
    customer_id,
    device_id,
    new_claim_code,
    new_enrollment_token,
    validate,
)
from registry.sqlite_provider import SQLiteRegistry  # noqa: E402


def fresh():
    reg = SQLiteRegistry(":memory:")
    reg.init_schema()
    return reg


def _customer(reg, slug="acme", subnet="10.50.0.0/24"):
    return reg.create_customer(Customer(
        customer_id=customer_id(slug),
        display_name=f"{slug} inc",
        vpn_subnet=subnet,
        hub_provider="lightsail",
    ))


def test_identity_formats():
    assert customer_id("acme") == "cust_acme"
    assert device_id("acme", 42) == "kln_acme_000042"
    assert validate("claim", new_claim_code())
    assert validate("enroll_token", new_enrollment_token())
    for bad in ["cust_Acme", "acme", "cust_acme!"]:
        try:
            validate("customer", bad)
            raise AssertionError(f"expected reject: {bad}")
        except ValueError:
            pass


def test_create_and_get_customer():
    reg = fresh()
    c = _customer(reg)
    assert c.customer_id == "cust_acme"
    assert reg.get_customer("cust_acme").vpn_subnet == "10.50.0.0/24"


def test_duplicate_customer_conflict():
    reg = fresh()
    _customer(reg)
    try:
        _customer(reg)
        raise AssertionError("expected Conflict")
    except Conflict:
        pass


def test_duplicate_subnet_conflict():
    reg = fresh()
    _customer(reg, "acme", "10.50.0.0/24")
    try:
        _customer(reg, "beta", "10.50.0.0/24")
        raise AssertionError("expected Conflict on duplicate subnet")
    except Conflict:
        pass


def test_ip_allocation_sequence():
    reg = fresh()
    _customer(reg)
    ips = [reg.allocate_ip("cust_acme") for _ in range(3)]
    assert ips == ["10.50.0.2", "10.50.0.3", "10.50.0.4"]


def test_ip_allocation_isolated_per_customer():
    reg = fresh()
    _customer(reg, "acme", "10.50.0.0/24")
    _customer(reg, "beta", "10.51.0.0/24")
    assert reg.allocate_ip("cust_acme") == "10.50.0.2"
    assert reg.allocate_ip("cust_beta") == "10.51.0.2"
    assert reg.allocate_ip("cust_acme") == "10.50.0.3"


def test_subnet_exhaustion():
    reg = fresh()
    _customer(reg)
    # Force the allocator near the top of the tower range.
    reg._conn.execute(
        "UPDATE ip_allocations SET next_host_octet = ? WHERE customer_id = ?",
        (reg.TOWER_OCTET_MAX, "cust_acme"),
    )
    reg._conn.commit()
    assert reg.allocate_ip("cust_acme") == "10.50.0.99"
    try:
        reg.allocate_ip("cust_acme")
        raise AssertionError("expected SubnetExhausted")
    except SubnetExhausted:
        pass


def test_register_and_enroll_tower():
    reg = fresh()
    _customer(reg)
    did = device_id("acme", 42)
    reg.register_tower(Tower(device_id=did, customer_id="cust_acme", claim_code=new_claim_code()))
    ip = reg.allocate_ip("cust_acme")
    t = reg.mark_tower_enrolled(did, wg_public_key="PUBKEY==", vpn_ip=ip)
    assert t.status == "enrolled"
    assert t.vpn_ip == "10.50.0.2"
    assert t.enrolled_at is not None


def test_tower_unknown_customer():
    reg = fresh()
    try:
        reg.register_tower(Tower(device_id=device_id("ghost", 1), customer_id="cust_ghost"))
        raise AssertionError("expected NotFound")
    except NotFound:
        pass


def test_claim_lookup_and_audit():
    reg = fresh()
    _customer(reg)
    claim = new_claim_code()
    did = device_id("acme", 7)
    reg.register_tower(Tower(device_id=did, customer_id="cust_acme", claim_code=claim))
    assert reg.get_tower_by_claim(claim).device_id == did
    reg.audit("test_event", entity_id=did, actor="pytest", payload_json={"k": "v"})
    rows = reg._conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()
    assert rows["c"] == 1


def _run_all():
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
