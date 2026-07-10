"""Tests for customer /24 subnet allocation."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.interface import SubnetExhausted  # noqa: E402
from registry.subnet import next_customer_subnet  # noqa: E402


def test_next_subnet_empty():
    assert next_customer_subnet([]) == "10.50.0.0/24"


def test_next_subnet_skips_used():
    assert next_customer_subnet(["10.50.0.0/24", "10.51.0.0/24"]) == "10.52.0.0/24"


def test_next_subnet_gap():
    assert next_customer_subnet(["10.50.0.0/24", "10.52.0.0/24"]) == "10.51.0.0/24"


def _run_all():
    failed = 0
    for fn in (test_next_subnet_empty, test_next_subnet_skips_used, test_next_subnet_gap):
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
