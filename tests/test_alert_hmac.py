"""Lock the alert HMAC contract between the tower watchdog and the hub listener.

Reproduces the watchdog signing (kallon_watchdog._sign) and asserts the hub
listener (infra/hub/alert_listener.verify) accepts it, and rejects tampering.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "infra", "hub"))

from alert_listener import verify  # type: ignore  # noqa: E402


def watchdog_sign(body: bytes, key: bytes) -> str:
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def main() -> int:
    key = b"dGhpcy1pcy1hLXRlc3Qta2V5LWJhc2U2NA=="  # arbitrary base64-looking key
    alert = {"device_id": "kln_lab_000001", "type": "tamper_impact", "ts": 1717600000}
    body = json.dumps(alert, sort_keys=True, separators=(",", ":")).encode()
    sig = watchdog_sign(body, key)

    failures = 0

    def check(cond, label):
        nonlocal failures
        print(("PASS " if cond else "FAIL ") + label)
        failures += 0 if cond else 1

    check(verify(body, sig, key), "valid signature accepted")
    check(not verify(body, "sha256=deadbeef", key), "bad signature rejected")
    check(not verify(body + b" ", sig, key), "tampered body rejected")
    check(not verify(body, sig, b"wrong-key"), "wrong key rejected")
    check(verify(body, sig.removeprefix("sha256="), key), "bare-hex signature accepted")

    print(f"\n{'ALL OK' if failures == 0 else str(failures) + ' FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
