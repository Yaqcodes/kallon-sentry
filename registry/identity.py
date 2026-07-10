"""Identity and secret formats for the Kallon fleet.

Single source of truth for the ID/secret string formats used across the
registry, enrollment API, hub provisioner, and factory scripts. Keep this in
sync with docs/identity-and-secrets.md.
"""
from __future__ import annotations

import base64
import os
import re
import secrets

# ── format regexes ───────────────────────────────────────────────────────────
CUSTOMER_RE = re.compile(r"^cust_[a-z0-9]+$")
DEVICE_RE = re.compile(r"^kln_[a-z0-9]+_\d{6}$")
GATEWAY_RE = re.compile(r"^gw_[a-z0-9]+$")
GROUP_RE = re.compile(r"^grp_[a-z0-9]+_[a-z0-9]+$")
CLAIM_RE = re.compile(r"^clm_[A-Za-z0-9_-]{22}$")          # base64url of 16 bytes
ENROLL_TOKEN_RE = re.compile(r"^enr_[A-Za-z0-9_-]{43}$")   # base64url of 32 bytes
SLUG_RE = re.compile(r"^[a-z0-9]+$")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# ── constructors ─────────────────────────────────────────────────────────────
def customer_id(slug: str) -> str:
    slug = slug.lower()
    if not SLUG_RE.match(slug):
        raise ValueError(f"customer slug must be [a-z0-9]+: {slug!r}")
    return f"cust_{slug}"


def device_id(slug: str, serial: int) -> str:
    slug = slug.lower()
    if not SLUG_RE.match(slug):
        raise ValueError(f"device slug must be [a-z0-9]+: {slug!r}")
    if not 0 <= serial <= 999999:
        raise ValueError("serial must be 0..999999")
    return f"kln_{slug}_{serial:06d}"


def gateway_id(slug: str) -> str:
    return f"gw_{slug.lower()}"


def group_id(slug: str, site: str) -> str:
    return f"grp_{slug.lower()}_{site.lower()}"


def new_claim_code() -> str:
    return "clm_" + _b64url(secrets.token_bytes(16))


def new_enrollment_token() -> str:
    return "enr_" + _b64url(secrets.token_bytes(32))


def new_alert_key() -> str:
    """32-byte HMAC key, standard base64 (matches openssl rand -base64 32)."""
    return base64.b64encode(os.urandom(32)).decode()


# ── validators ───────────────────────────────────────────────────────────────
def slug_of(cust_id: str) -> str:
    if not CUSTOMER_RE.match(cust_id):
        raise ValueError(f"not a customer_id: {cust_id!r}")
    return cust_id[len("cust_"):]


def validate(kind: str, value: str) -> str:
    table = {
        "customer": CUSTOMER_RE,
        "device": DEVICE_RE,
        "gateway": GATEWAY_RE,
        "group": GROUP_RE,
        "claim": CLAIM_RE,
        "enroll_token": ENROLL_TOKEN_RE,
    }
    rx = table.get(kind)
    if rx is None:
        raise ValueError(f"unknown id kind: {kind}")
    if not rx.match(value):
        raise ValueError(f"{kind} id {value!r} does not match {rx.pattern}")
    return value
