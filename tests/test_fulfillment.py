"""Tests for order fulfillment (SQLite, dry-run)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["KALLON_REGISTRY"] = "sqlite"

from infra.fulfillment.cli import fulfill_order  # noqa: E402
from infra.fulfillment.device_env import factory_camera_ips, render_device_env  # noqa: E402
from registry import get_registry  # noqa: E402
from registry.identity import customer_id  # noqa: E402


def test_factory_camera_ips():
    assert factory_camera_ips(2) == "192.168.10.108,192.168.10.109"


def test_render_device_env():
    text = render_device_env(
        device_id="kln_acme_000001",
        customer_id="cust_acme",
        claim_code="clm_test",
        enrollment_token="enr_test",
        enrollment_url="https://enroll.example/v1",
        cameras=2,
    )
    assert "CAMERA_IPS=192.168.10.108,192.168.10.109" in text
    assert "ENROLLMENT_URL=https://enroll.example/v1" in text


def test_fulfill_order_dry_run_new_customer():
    plan = fulfill_order(
        "beta",
        display_name="Beta Co",
        towers=2,
        cameras=1,
        provider="manual",
        host="203.0.113.1",
        enrollment_url="https://enroll.example/v1",
        dry_run=True,
        registry_name="sqlite",
    )
    assert plan["customer_id"] == "cust_beta"
    assert plan["subnet"] == "10.50.0.0/24"
    assert plan["hub_provision_needed"] is True
    assert len(plan["units"]) == 2
    assert plan["units"][0]["device_id"] == "kln_beta_000001"


def test_fulfill_order_skips_hub_if_active(tmp_path):
    db = tmp_path / "gamma.db"
    os.environ["KALLON_SQLITE_PATH"] = str(db)
    reg = get_registry("sqlite")
    from registry import Customer  # noqa: E402

    reg.create_customer(Customer(
        customer_id="cust_gamma",
        display_name="Gamma",
        vpn_subnet="10.55.0.0/24",
        hub_provider="manual",
        gateway_endpoint="1.2.3.4:51820",
        gateway_public_key="KEY==",
        status="active",
    ))
    reg.close()

    plan = fulfill_order(
        "gamma",
        display_name="Gamma",
        towers=1,
        cameras=1,
        provider="manual",
        host="1.2.3.4",
        enrollment_url="https://enroll.example/v1",
        dry_run=True,
        registry_name="sqlite",
    )
    assert plan["hub_provision_needed"] is False
    assert plan["subnet"] == "10.55.0.0/24"
    os.environ.pop("KALLON_SQLITE_PATH", None)


def _run_all():
    import tempfile
    from pathlib import Path

    failed = 0
    tests = [
        test_factory_camera_ips,
        test_render_device_env,
        test_fulfill_order_dry_run_new_customer,
    ]
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            test_fulfill_order_skips_hub_if_active(Path(tmp))
        print("PASS test_fulfill_order_skips_hub_if_active")
    except Exception as e:  # noqa: BLE001
        failed += 1
        print(f"FAIL test_fulfill_order_skips_hub_if_active: {e!r}")
    print(f"\n{4 - failed}/4 passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
