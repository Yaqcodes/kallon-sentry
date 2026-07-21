"""PostgreSQL RegistryProvider — production store on the Terra physical server.

Requires psycopg (v3). Configure with DATABASE_URL (LAN / ops-VPN only; never
public). Applies migrations/001_initial.sql on init_schema().
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime

from typing import Any, Optional

try:  # psycopg is only needed in production, not for SQLite unit tests.
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore

from datetime import datetime

from .interface import (
    Conflict,
    Customer,
    NotFound,
    RecordingSegment,
    RegistryProvider,
    SubnetExhausted,
    Tower,
)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

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
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
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

    # ── recordings ───────────────────────────────────────────────────────────
    def _row_to_segment(self, row: dict) -> RecordingSegment:
        return RecordingSegment(**row)

    def upsert_recording_segment(self, segment: RecordingSegment) -> RecordingSegment:
        cols = [
            "segment_id", "customer_id", "device_id", "camera", "filename",
            "s3_bucket", "s3_key", "size_bytes", "sha256_hex", "started_at",
            "ended_at", "uploaded_at", "duration_sec",
        ]
        values = tuple(getattr(segment, c) for c in cols)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO recording_segments ({', '.join(cols)})
                    VALUES ({', '.join(['%s'] * len(cols))})
                    ON CONFLICT (device_id, camera, filename) DO UPDATE SET
                        s3_bucket = EXCLUDED.s3_bucket,
                        s3_key = EXCLUDED.s3_key,
                        size_bytes = EXCLUDED.size_bytes,
                        sha256_hex = EXCLUDED.sha256_hex,
                        started_at = EXCLUDED.started_at,
                        ended_at = EXCLUDED.ended_at,
                        uploaded_at = EXCLUDED.uploaded_at,
                        duration_sec = EXCLUDED.duration_sec
                    RETURNING *
                    """,
                    values,
                )
                row = cur.fetchone()
            self._conn.commit()
        except psycopg.errors.ForeignKeyViolation as e:  # type: ignore[attr-defined]
            self._conn.rollback()
            raise NotFound(f"customer or tower for segment {segment.segment_id}") from e
        assert row is not None
        return self._row_to_segment(row)

    def get_recording_segment(self, segment_id: str) -> RecordingSegment:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM recording_segments WHERE segment_id = %s", (segment_id,))
            row = cur.fetchone()
        if not row:
            raise NotFound(f"recording segment {segment_id}")
        return self._row_to_segment(row)

    def delete_recording_segment(self, segment_id: str) -> RecordingSegment:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recording_segments WHERE segment_id = %s RETURNING *",
                (segment_id,),
            )
            row = cur.fetchone()
        if not row:
            self._conn.rollback()
            raise NotFound(f"recording segment {segment_id}")
        self._conn.commit()
        return self._row_to_segment(row)

    def list_recording_segments(
        self,
        *,
        customer_id: str,
        device_id: Optional[str] = None,
        camera: Optional[int] = None,
        started_after: Optional[datetime] = None,
        started_before: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RecordingSegment]:
        clauses = ["customer_id = %s"]
        params: list[Any] = [customer_id]
        if device_id:
            clauses.append("device_id = %s")
            params.append(device_id)
        if camera is not None:
            clauses.append("camera = %s")
            params.append(camera)
        if started_after is not None:
            clauses.append("started_at >= %s")
            params.append(started_after)
        if started_before is not None:
            clauses.append("started_at <= %s")
            params.append(started_before)
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        params.extend([limit, offset])
        sql = (
            f"SELECT * FROM recording_segments WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at DESC LIMIT %s OFFSET %s"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [self._row_to_segment(r) for r in rows]

    def list_expired_recording_segments(
        self, *, retention_days: int, limit: int = 200
    ) -> list[RecordingSegment]:
        limit = max(1, min(limit, 1000))
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM recording_segments
                WHERE started_at < now() - make_interval(days => %s)
                ORDER BY started_at ASC
                LIMIT %s
                """,
                (retention_days, limit),
            )
            rows = cur.fetchall()
        return [self._row_to_segment(r) for r in rows]

    def get_platform_config(self, key: str) -> Optional[str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM platform_config WHERE key = %s", (key,))
            row = cur.fetchone()
        return row["value"] if row else None

    def set_platform_config(self, key: str, value: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_config (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, value),
            )
        self._conn.commit()
