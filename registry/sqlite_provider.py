"""SQLite RegistryProvider — UNIT TESTS ONLY.

Not for factory or field use (the production store is Postgres on the Terra
physical server). This provider mirrors the schema in migrations/001_initial.sql
with SQLite-compatible types so the same business logic can be exercised
off-line and in CI.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .interface import (
    Conflict,
    Customer,
    NotFound,
    RegistryProvider,
    SubnetExhausted,
    Tower,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    vpn_subnet TEXT NOT NULL UNIQUE,
    gateway_id TEXT,
    gateway_endpoint TEXT,
    gateway_public_key TEXT,
    hub_alert_url TEXT,
    hub_provider TEXT NOT NULL DEFAULT 'manual',
    hub_host_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending_hub',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS towers (
    device_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    group_id TEXT,
    vpn_ip TEXT UNIQUE,
    wg_public_key TEXT,
    claim_code TEXT UNIQUE,
    enrollment_token_hash TEXT,
    manufactured_at TEXT NOT NULL,
    enrolled_at TEXT,
    acceptance_status TEXT NOT NULL DEFAULT 'pending',
    status TEXT NOT NULL DEFAULT 'manufactured',
    shipped_at TEXT
);
CREATE TABLE IF NOT EXISTS ip_allocations (
    customer_id TEXT PRIMARY KEY REFERENCES customers(customer_id),
    next_host_octet INTEGER NOT NULL DEFAULT 2
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_id TEXT,
    actor TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL
);
"""

_CUSTOMER_COLS = [
    "customer_id", "display_name", "vpn_subnet", "gateway_id", "gateway_endpoint",
    "gateway_public_key", "hub_alert_url", "hub_provider", "hub_host_id",
    "status", "created_at",
]
_TOWER_COLS = [
    "device_id", "customer_id", "group_id", "vpn_ip", "wg_public_key", "claim_code",
    "enrollment_token_hash", "manufactured_at", "enrolled_at", "acceptance_status",
    "status", "shipped_at",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


class SQLiteRegistry(RegistryProvider):
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── customers ────────────────────────────────────────────────────────────
    def create_customer(self, customer: Customer) -> Customer:
        if customer.created_at is None:
            customer.created_at = datetime.now(timezone.utc)
        try:
            self._conn.execute(
                f"INSERT INTO customers ({','.join(_CUSTOMER_COLS)}) "
                f"VALUES ({','.join('?' for _ in _CUSTOMER_COLS)})",
                (
                    customer.customer_id, customer.display_name, customer.vpn_subnet,
                    customer.gateway_id, customer.gateway_endpoint, customer.gateway_public_key,
                    customer.hub_alert_url, customer.hub_provider, customer.hub_host_id,
                    customer.status, customer.created_at.isoformat(),
                ),
            )
            self._conn.execute(
                "INSERT INTO ip_allocations (customer_id, next_host_octet) VALUES (?, ?)",
                (customer.customer_id, self.TOWER_OCTET_MIN),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as e:
            raise Conflict(str(e)) from e
        return self.get_customer(customer.customer_id)

    def get_customer(self, customer_id: str) -> Customer:
        row = self._conn.execute(
            "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"customer {customer_id}")
        return self._row_to_customer(row)

    def update_customer_hub(self, customer_id: str, **fields) -> Customer:
        self.get_customer(customer_id)  # existence check
        allowed = {
            "gateway_id", "gateway_endpoint", "gateway_public_key", "hub_alert_url",
            "hub_provider", "hub_host_id", "status", "display_name",
        }
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            self._conn.execute(
                f"UPDATE customers SET {cols} WHERE customer_id = ?",
                (*sets.values(), customer_id),
            )
            self._conn.commit()
        return self.get_customer(customer_id)

    def list_customers(self) -> list[Customer]:
        rows = self._conn.execute("SELECT * FROM customers ORDER BY created_at").fetchall()
        return [self._row_to_customer(r) for r in rows]

    # ── towers ───────────────────────────────────────────────────────────────
    def register_tower(self, tower: Tower) -> Tower:
        self.get_customer(tower.customer_id)  # FK existence
        if tower.manufactured_at is None:
            tower.manufactured_at = datetime.now(timezone.utc)
        try:
            self._conn.execute(
                f"INSERT INTO towers ({','.join(_TOWER_COLS)}) "
                f"VALUES ({','.join('?' for _ in _TOWER_COLS)})",
                (
                    tower.device_id, tower.customer_id, tower.group_id, tower.vpn_ip,
                    tower.wg_public_key, tower.claim_code, tower.enrollment_token_hash,
                    tower.manufactured_at.isoformat(),
                    tower.enrolled_at.isoformat() if tower.enrolled_at else None,
                    tower.acceptance_status, tower.status,
                    tower.shipped_at.isoformat() if tower.shipped_at else None,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as e:
            raise Conflict(str(e)) from e
        return self.get_tower(tower.device_id)

    def get_tower(self, device_id: str) -> Tower:
        row = self._conn.execute(
            "SELECT * FROM towers WHERE device_id = ?", (device_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"tower {device_id}")
        return self._row_to_tower(row)

    def get_tower_by_claim(self, claim_code: str) -> Tower:
        row = self._conn.execute(
            "SELECT * FROM towers WHERE claim_code = ?", (claim_code,)
        ).fetchone()
        if not row:
            raise NotFound(f"tower with claim {claim_code}")
        return self._row_to_tower(row)

    def list_towers(self, customer_id: Optional[str] = None) -> list[Tower]:
        if customer_id:
            rows = self._conn.execute(
                "SELECT * FROM towers WHERE customer_id = ? ORDER BY device_id",
                (customer_id,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM towers ORDER BY device_id").fetchall()
        return [self._row_to_tower(r) for r in rows]

    def mark_tower_enrolled(self, device_id: str, *, wg_public_key: str, vpn_ip: str) -> Tower:
        self.get_tower(device_id)
        self._conn.execute(
            "UPDATE towers SET wg_public_key = ?, vpn_ip = ?, enrolled_at = ?, status = 'enrolled' "
            "WHERE device_id = ?",
            (wg_public_key, vpn_ip, _now(), device_id),
        )
        self._conn.commit()
        return self.get_tower(device_id)

    def set_tower_status(self, device_id: str, status: str) -> Tower:
        self.get_tower(device_id)
        self._conn.execute(
            "UPDATE towers SET status = ? WHERE device_id = ?", (status, device_id)
        )
        self._conn.commit()
        return self.get_tower(device_id)

    def set_tower_acceptance(self, device_id: str, acceptance_status: str) -> Tower:
        self.get_tower(device_id)
        self._conn.execute(
            "UPDATE towers SET acceptance_status = ? WHERE device_id = ?",
            (acceptance_status, device_id),
        )
        self._conn.commit()
        return self.get_tower(device_id)

    # ── IP allocation ────────────────────────────────────────────────────────
    def allocate_ip(self, customer_id: str) -> str:
        cust = self.get_customer(customer_id)
        cur = self._conn.execute(
            "SELECT next_host_octet FROM ip_allocations WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        octet = cur["next_host_octet"] if cur else self.TOWER_OCTET_MIN
        if octet > self.TOWER_OCTET_MAX:
            raise SubnetExhausted(f"{customer_id} exhausted tower range")
        self._conn.execute(
            "UPDATE ip_allocations SET next_host_octet = ? WHERE customer_id = ?",
            (octet + 1, customer_id),
        )
        self._conn.commit()
        return self.host_ip(cust.vpn_subnet, octet)

    # ── audit ────────────────────────────────────────────────────────────────
    def audit(self, event_type: str, *, entity_id=None, actor=None, payload_json=None) -> None:
        if payload_json is not None and not isinstance(payload_json, str):
            payload_json = json.dumps(payload_json)
        self._conn.execute(
            "INSERT INTO audit_events (event_type, entity_id, actor, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, entity_id, actor, payload_json, _now()),
        )
        self._conn.commit()

    # ── row mappers ──────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_customer(row: sqlite3.Row) -> Customer:
        d = dict(row)
        d["created_at"] = _parse_dt(d.get("created_at"))
        return Customer(**d)

    @staticmethod
    def _row_to_tower(row: sqlite3.Row) -> Tower:
        d = dict(row)
        for k in ("manufactured_at", "enrolled_at", "shipped_at"):
            d[k] = _parse_dt(d.get(k))
        return Tower(**d)
