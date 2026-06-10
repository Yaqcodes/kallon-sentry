"""PostgreSQL RegistryProvider — production store on the Terra physical server.

Requires psycopg (v3). Configure with DATABASE_URL (LAN / ops-VPN only; never
public). Applies migrations/001_initial.sql on init_schema().
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

try:  # psycopg is only needed in production, not for SQLite unit tests.
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore

from .interface import (
    Conflict,
    Customer,
    NotFound,
    RegistryProvider,
    SubnetExhausted,
    Tower,
)

_MIGRATION = Path(__file__).parent / "migrations" / "001_initial.sql"

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


class PostgresRegistry(RegistryProvider):
    def __init__(self, dsn: Optional[str] = None) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed; pip install 'psycopg[binary]'")
        self._dsn = dsn or os.environ.get("DATABASE_URL")
        if not self._dsn:
            raise RuntimeError("DATABASE_URL not set and no dsn provided")
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def init_schema(self) -> None:
        sql = _MIGRATION.read_text(encoding="utf-8")
        with self._conn.cursor() as cur:
            cur.execute(sql)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── customers ────────────────────────────────────────────────────────────
    def create_customer(self, customer: Customer) -> Customer:
        cols = [c for c in _CUSTOMER_COLS if c != "created_at"]
        placeholders = ", ".join(["%s"] * len(cols))
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO customers ({', '.join(cols)}) VALUES ({placeholders})",
                    tuple(getattr(customer, c) for c in cols),
                )
                cur.execute(
                    "INSERT INTO ip_allocations (customer_id, next_host_octet) VALUES (%s, %s)",
                    (customer.customer_id, self.TOWER_OCTET_MIN),
                )
            self._conn.commit()
        except psycopg.errors.UniqueViolation as e:  # type: ignore[attr-defined]
            self._conn.rollback()
            raise Conflict(str(e)) from e
        return self.get_customer(customer.customer_id)

    def get_customer(self, customer_id: str) -> Customer:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM customers WHERE customer_id = %s", (customer_id,))
            row = cur.fetchone()
        if not row:
            raise NotFound(f"customer {customer_id}")
        return Customer(**row)

    def update_customer_hub(self, customer_id: str, **fields) -> Customer:
        allowed = {
            "gateway_id", "gateway_endpoint", "gateway_public_key", "hub_alert_url",
            "hub_provider", "hub_host_id", "status", "display_name",
        }
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ", ".join(f"{k} = %s" for k in sets)
            with self._conn.cursor() as cur:
                cur.execute(
                    f"UPDATE customers SET {cols} WHERE customer_id = %s",
                    (*sets.values(), customer_id),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    raise NotFound(f"customer {customer_id}")
            self._conn.commit()
        else:
            self.get_customer(customer_id)
        return self.get_customer(customer_id)

    def list_customers(self) -> list[Customer]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM customers ORDER BY created_at")
            return [Customer(**r) for r in cur.fetchall()]

    # ── towers ───────────────────────────────────────────────────────────────
    def register_tower(self, tower: Tower) -> Tower:
        cols = [c for c in _TOWER_COLS if c not in ("manufactured_at",)]
        placeholders = ", ".join(["%s"] * len(cols))
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO towers ({', '.join(cols)}) VALUES ({placeholders})",
                    tuple(getattr(tower, c) for c in cols),
                )
            self._conn.commit()
        except psycopg.errors.UniqueViolation as e:  # type: ignore[attr-defined]
            self._conn.rollback()
            raise Conflict(str(e)) from e
        except psycopg.errors.ForeignKeyViolation as e:  # type: ignore[attr-defined]
            self._conn.rollback()
            raise NotFound(f"customer {tower.customer_id}") from e
        return self.get_tower(tower.device_id)

    def get_tower(self, device_id: str) -> Tower:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM towers WHERE device_id = %s", (device_id,))
            row = cur.fetchone()
        if not row:
            raise NotFound(f"tower {device_id}")
        return Tower(**row)

    def get_tower_by_claim(self, claim_code: str) -> Tower:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM towers WHERE claim_code = %s", (claim_code,))
            row = cur.fetchone()
        if not row:
            raise NotFound(f"tower with claim {claim_code}")
        return Tower(**row)

    def list_towers(self, customer_id: Optional[str] = None) -> list[Tower]:
        with self._conn.cursor() as cur:
            if customer_id:
                cur.execute(
                    "SELECT * FROM towers WHERE customer_id = %s ORDER BY device_id",
                    (customer_id,),
                )
            else:
                cur.execute("SELECT * FROM towers ORDER BY device_id")
            return [Tower(**r) for r in cur.fetchall()]

    def mark_tower_enrolled(self, device_id: str, *, wg_public_key: str, vpn_ip: str) -> Tower:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE towers SET wg_public_key = %s, vpn_ip = %s, enrolled_at = now(), "
                "status = 'enrolled' WHERE device_id = %s",
                (wg_public_key, vpn_ip, device_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise NotFound(f"tower {device_id}")
        self._conn.commit()
        return self.get_tower(device_id)

    def set_tower_status(self, device_id: str, status: str) -> Tower:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE towers SET status = %s WHERE device_id = %s", (status, device_id)
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise NotFound(f"tower {device_id}")
        self._conn.commit()
        return self.get_tower(device_id)

    def set_tower_acceptance(self, device_id: str, acceptance_status: str) -> Tower:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE towers SET acceptance_status = %s WHERE device_id = %s",
                (acceptance_status, device_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise NotFound(f"tower {device_id}")
        self._conn.commit()
        return self.get_tower(device_id)

    # ── IP allocation (row-locked, atomic) ───────────────────────────────────
    def allocate_ip(self, customer_id: str) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT vpn_subnet FROM customers WHERE customer_id = %s", (customer_id,)
            )
            crow = cur.fetchone()
            if not crow:
                self._conn.rollback()
                raise NotFound(f"customer {customer_id}")
            cur.execute(
                "SELECT next_host_octet FROM ip_allocations WHERE customer_id = %s FOR UPDATE",
                (customer_id,),
            )
            arow = cur.fetchone()
            octet = arow["next_host_octet"] if arow else self.TOWER_OCTET_MIN
            if octet > self.TOWER_OCTET_MAX:
                self._conn.rollback()
                raise SubnetExhausted(f"{customer_id} exhausted tower range")
            cur.execute(
                "UPDATE ip_allocations SET next_host_octet = %s WHERE customer_id = %s",
                (octet + 1, customer_id),
            )
        self._conn.commit()
        return self.host_ip(crow["vpn_subnet"], octet)

    # ── audit ────────────────────────────────────────────────────────────────
    def audit(self, event_type: str, *, entity_id=None, actor=None, payload_json=None) -> None:
        if payload_json is not None and not isinstance(payload_json, str):
            payload_json = json.dumps(payload_json)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_events (event_type, entity_id, actor, payload_json) "
                "VALUES (%s, %s, %s, %s)",
                (event_type, entity_id, actor, payload_json),
            )
        self._conn.commit()
