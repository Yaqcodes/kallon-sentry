"""RegistryProvider interface and domain models.

Both the production Postgres provider and the unit-test SQLite provider
implement `RegistryProvider`. Factory scripts and the enrollment API depend
only on this interface — never on raw SQL.
"""
from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Optional


@dataclass
class Customer:
    customer_id: str
    display_name: str
    vpn_subnet: str
    gateway_id: Optional[str] = None
    gateway_endpoint: Optional[str] = None
    gateway_public_key: Optional[str] = None
    hub_alert_url: Optional[str] = None
    hub_provider: str = "manual"
    hub_host_id: Optional[str] = None
    status: str = "pending_hub"
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecordingSegment:
    segment_id: str
    customer_id: str
    device_id: str
    camera: int
    filename: str
    s3_bucket: str
    s3_key: str
    size_bytes: int
    started_at: datetime
    sha256_hex: Optional[str] = None
    ended_at: Optional[datetime] = None
    uploaded_at: Optional[datetime] = None
    duration_sec: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "customer_id": self.customer_id,
            "device_id": self.device_id,
            "camera": self.camera,
            "filename": self.filename,
            "s3_bucket": self.s3_bucket,
            "s3_key": self.s3_key,
            "size_bytes": self.size_bytes,
            "sha256_hex": self.sha256_hex,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "duration_sec": self.duration_sec,
        }


@dataclass
class Tower:
    device_id: str
    customer_id: str
    group_id: Optional[str] = None
    vpn_ip: Optional[str] = None
    wg_public_key: Optional[str] = None
    claim_code: Optional[str] = None
    enrollment_token_hash: Optional[str] = None
    manufactured_at: Optional[datetime] = None
    enrolled_at: Optional[datetime] = None
    acceptance_status: str = "pending"
    status: str = "manufactured"
    shipped_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RegistryError(Exception):
    """Base class for registry errors."""


class NotFound(RegistryError):
    pass


class Conflict(RegistryError):
    pass


class SubnetExhausted(RegistryError):
    pass


class RegistryProvider(ABC):
    """Storage-agnostic registry operations."""

    # ── lifecycle ────────────────────────────────────────────────────────────
    @abstractmethod
    def init_schema(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    # ── customers ────────────────────────────────────────────────────────────
    @abstractmethod
    def create_customer(self, customer: Customer) -> Customer: ...

    @abstractmethod
    def get_customer(self, customer_id: str) -> Customer: ...

    @abstractmethod
    def update_customer_hub(
        self,
        customer_id: str,
        *,
        gateway_id: Optional[str] = None,
        gateway_endpoint: Optional[str] = None,
        gateway_public_key: Optional[str] = None,
        hub_alert_url: Optional[str] = None,
        hub_provider: Optional[str] = None,
        hub_host_id: Optional[str] = None,
        status: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Customer: ...

    @abstractmethod
    def list_customers(self) -> list[Customer]: ...

    # ── towers ───────────────────────────────────────────────────────────────
    @abstractmethod
    def register_tower(self, tower: Tower) -> Tower: ...

    @abstractmethod
    def get_tower(self, device_id: str) -> Tower: ...

    @abstractmethod
    def get_tower_by_claim(self, claim_code: str) -> Tower: ...

    @abstractmethod
    def list_towers(self, customer_id: Optional[str] = None) -> list[Tower]: ...

    @abstractmethod
    def mark_tower_enrolled(
        self, device_id: str, *, wg_public_key: str, vpn_ip: str
    ) -> Tower: ...

    @abstractmethod
    def set_tower_status(self, device_id: str, status: str) -> Tower: ...

    @abstractmethod
    def set_tower_acceptance(self, device_id: str, acceptance_status: str) -> Tower: ...

    # ── IP allocation ────────────────────────────────────────────────────────
    @abstractmethod
    def allocate_ip(self, customer_id: str) -> str:
        """Allocate the next tower /32 host address in the customer subnet."""

    # ── audit ────────────────────────────────────────────────────────────────
    @abstractmethod
    def audit(
        self,
        event_type: str,
        *,
        entity_id: Optional[str] = None,
        actor: Optional[str] = None,
        payload_json: Optional[str] = None,
    ) -> None: ...

    # ── recordings (S3 segment registry) ───────────────────────────────────
    @abstractmethod
    def upsert_recording_segment(self, segment: RecordingSegment) -> RecordingSegment: ...

    @abstractmethod
    def get_recording_segment(self, segment_id: str) -> RecordingSegment: ...

    @abstractmethod
    def delete_recording_segment(self, segment_id: str) -> RecordingSegment: ...

    @abstractmethod
    def delete_recording_segments_for_device(self, device_id: str) -> int: ...

    @abstractmethod
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
    ) -> list[RecordingSegment]: ...

    @abstractmethod
    def list_expired_recording_segments(
        self, *, retention_days: int, limit: int = 200
    ) -> list[RecordingSegment]: ...

    @abstractmethod
    def get_platform_config(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def set_platform_config(self, key: str, value: str) -> None: ...

    # ── shared helpers (non-abstract) ────────────────────────────────────────
    @staticmethod
    def host_ip(subnet: str, octet: int) -> str:
        """Return the dotted host address for the given /24-style subnet + octet."""
        net = ipaddress.ip_network(subnet, strict=False)
        base = int(net.network_address)
        return str(ipaddress.ip_address(base + octet))

    # Tower host-octet range within each customer subnet (see roadmap §3).
    TOWER_OCTET_MIN = 2
    TOWER_OCTET_MAX = 99
