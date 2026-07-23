"""Recording segment registry + S3 presigned playback for buyer dashboards.

Tower upload workers POST metadata after a verified S3 put (see
scripts/kallon-recording-uploader.py). Customers list/query/delete only
segments belonging to their customer_id — enforced on every route.

Contract: docs/platform-api.md §3c. OpenAPI: GET /docs (tag Recordings).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from registry import NotFound, RegistryError, get_registry
from registry.interface import RecordingSegment

from .platform import _auth_check, _err
from . import s3_storage

log = logging.getLogger("recordings")

router = APIRouter(prefix="/v1", tags=["Recordings"])

RECORDING_INGEST_TOKEN = os.environ.get("KALLON_RECORDING_INGEST_TOKEN", "").strip()


# ── OpenAPI models ───────────────────────────────────────────────────────────

class RecordingSegmentPublic(BaseModel):
    """Public segment metadata (S3 bucket/key intentionally omitted)."""

    segment_id: str
    customer_id: str
    device_id: str
    camera: int
    filename: str
    size_bytes: int
    sha256_hex: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    uploaded_at: Optional[str] = None
    duration_sec: Optional[int] = None


class RecordingIngestRequest(BaseModel):
    device_id: str = Field(..., description="Tower device_id (must already exist in registry)")
    camera: int = Field(..., ge=1, le=32)
    filename: str
    s3_bucket: str
    s3_key: str
    size_bytes: int = Field(..., ge=1)
    sha256_hex: Optional[str] = None
    started_at: str = Field(..., description="ISO-8601 UTC segment start")
    ended_at: Optional[str] = None
    duration_sec: Optional[int] = Field(default=None, ge=1)


class RecordingIngestResponse(BaseModel):
    segment: RecordingSegmentPublic


class RecordingListResponse(BaseModel):
    customer_id: str
    retention_days: int
    segments: list[RecordingSegmentPublic]


class RecordingGetResponse(BaseModel):
    segment: RecordingSegmentPublic


class PresignResponse(BaseModel):
    segment_id: str
    url: str
    expires_in: int


class DeleteResponse(BaseModel):
    status: str
    segment_id: str


class PurgeDeviceRequest(BaseModel):
    device_id: str = Field(..., description="Tower whose registry rows should be removed")


class PurgeDeviceResponse(BaseModel):
    device_id: str
    deleted_segments: int


class RetentionResponse(BaseModel):
    retention_days: int


class RetentionUpdateRequest(BaseModel):
    retention_days: int = Field(..., ge=1, description="Cloud retention window in days")


def _retention_days(reg) -> int:
    env_raw = os.environ.get("KALLON_RECORDING_RETENTION_DAYS", "").strip()
    if env_raw:
        try:
            return max(1, int(env_raw))
        except ValueError:
            pass
    cfg = reg.get_platform_config("recording_retention_days")
    if cfg:
        try:
            return max(1, int(cfg))
        except ValueError:
            pass
    return 30


def _ingest_auth(request: Request) -> Optional[JSONResponse]:
    token = RECORDING_INGEST_TOKEN or os.environ.get("KALLON_ALERT_INGEST_TOKEN", "").strip()
    if not token:
        return None
    provided = request.headers.get("X-Kallon-Ingest-Token", "")
    if provided != token:
        return _err(401, "unauthorized", "missing or invalid X-Kallon-Ingest-Token")
    return None


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _segment_public(seg: RecordingSegment) -> dict[str, Any]:
    return {
        "segment_id": seg.segment_id,
        "customer_id": seg.customer_id,
        "device_id": seg.device_id,
        "camera": seg.camera,
        "filename": seg.filename,
        "size_bytes": seg.size_bytes,
        "sha256_hex": seg.sha256_hex,
        "started_at": seg.started_at.isoformat() if seg.started_at else None,
        "ended_at": seg.ended_at.isoformat() if seg.ended_at else None,
        "uploaded_at": seg.uploaded_at.isoformat() if seg.uploaded_at else None,
        "duration_sec": seg.duration_sec,
    }


def _load_segment_for_customer(segment_id: str, customer_id: str, reg) -> tuple[Optional[RecordingSegment], Optional[JSONResponse]]:
    try:
        seg = reg.get_recording_segment(segment_id)
    except NotFound:
        return None, _err(404, "not_found", f"recording segment {segment_id!r} not found")
    except RegistryError as e:
        log.exception("registry error loading segment %s", segment_id)
        return None, _err(503, "registry_unavailable", str(e))
    if seg.customer_id != customer_id:
        return None, _err(404, "not_found", f"recording segment {segment_id!r} not found")
    return seg, None


@router.post(
    "/recordings/ingest",
    status_code=201,
    response_model=RecordingIngestResponse,
    summary="Ingest uploaded segment metadata",
    description=(
        "Tower upload worker registers a verified S3/B2 object after put. "
        "Auth: `X-Kallon-Ingest-Token` when `KALLON_RECORDING_INGEST_TOKEN` "
        "(or fallback `KALLON_ALERT_INGEST_TOKEN`) is set."
    ),
)
async def ingest_recording(request: Request):
    if (resp := _ingest_auth(request)) is not None:
        return resp
    try:
        payload = RecordingIngestRequest.model_validate_json(await request.body() or b"{}")
    except ValidationError as e:
        return _err(422, "invalid_request", f"invalid request body: {e.errors()}")

    started = _parse_dt(payload.started_at)
    if started is None:
        return _err(422, "invalid_request", "started_at must be ISO-8601 UTC")

    reg = get_registry()
    try:
        tower = reg.get_tower(payload.device_id)
    except NotFound:
        reg.close()
        return _err(404, "not_found", f"unknown device_id {payload.device_id!r}")
    except RegistryError as e:
        reg.close()
        return _err(503, "registry_unavailable", str(e))

    segment = RecordingSegment(
        segment_id=str(uuid.uuid4()),
        customer_id=tower.customer_id,
        device_id=payload.device_id,
        camera=payload.camera,
        filename=payload.filename,
        s3_bucket=payload.s3_bucket,
        s3_key=payload.s3_key,
        size_bytes=payload.size_bytes,
        sha256_hex=payload.sha256_hex,
        started_at=started,
        ended_at=_parse_dt(payload.ended_at),
        uploaded_at=datetime.now(timezone.utc),
        duration_sec=payload.duration_sec,
    )
    try:
        saved = reg.upsert_recording_segment(segment)
        reg.audit(
            "recording.ingest",
            entity_id=saved.segment_id,
            actor=payload.device_id,
            payload_json={"device_id": payload.device_id, "filename": payload.filename},
        )
    except RegistryError as e:
        reg.close()
        return _err(503, "registry_unavailable", str(e))
    reg.close()
    return JSONResponse(status_code=201, content={"segment": _segment_public(saved)})


@router.post(
    "/recordings/purge-device",
    response_model=PurgeDeviceResponse,
    summary="Purge device recording registry rows",
    description=(
        "Ops/reset: delete all `recording_segments` rows for a device. "
        "**Does not delete S3/B2 objects** — purge the bucket separately if needed. "
        "Auth: same ingest token as `/v1/recordings/ingest`."
    ),
)
async def purge_device_recordings(request: Request):
    if (resp := _ingest_auth(request)) is not None:
        return resp
    try:
        payload = PurgeDeviceRequest.model_validate_json(await request.body() or b"{}")
    except ValidationError as e:
        return _err(422, "invalid_request", f"invalid request body: {e.errors()}")

    device_id = payload.device_id.strip()
    if not device_id:
        return _err(422, "invalid_request", "device_id is required")

    reg = get_registry()
    try:
        tower = reg.get_tower(device_id)
        deleted = reg.delete_recording_segments_for_device(device_id)
        reg.audit(
            "recording.purge_device",
            entity_id=device_id,
            actor="ingest",
            payload_json={"deleted": deleted, "customer_id": tower.customer_id},
        )
    except NotFound:
        reg.close()
        return _err(404, "not_found", f"unknown device_id {device_id!r}")
    except RegistryError as e:
        reg.close()
        return _err(503, "registry_unavailable", str(e))
    reg.close()
    return {"device_id": device_id, "deleted_segments": deleted}


@router.get(
    "/customers/{customer_id}/recordings",
    response_model=RecordingListResponse,
    summary="List customer recording segments",
    description=(
        "Tenant-scoped list. Filters apply to `started_at`. "
        "`s3_bucket` / `s3_key` are not returned. Auth: `X-Kallon-Api-Key` when configured."
    ),
)
def list_customer_recordings(
    customer_id: str,
    request: Request,
    device_id: Optional[str] = Query(default=None, description="Filter by tower device_id"),
    camera: Optional[int] = Query(default=None, ge=1, le=32, description="Filter by camera index"),
    from_ts: Optional[str] = Query(default=None, description="ISO-8601 lower bound on started_at"),
    to_ts: Optional[str] = Query(default=None, description="ISO-8601 upper bound on started_at"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        reg.get_customer(customer_id)
        if device_id:
            tower = reg.get_tower(device_id)
            if tower.customer_id != customer_id:
                reg.close()
                return _err(404, "not_found", f"tower {device_id!r} not found for customer")
        rows = reg.list_recording_segments(
            customer_id=customer_id,
            device_id=device_id,
            camera=camera,
            started_after=_parse_dt(from_ts),
            started_before=_parse_dt(to_ts),
            limit=limit,
            offset=offset,
        )
        retention_days = _retention_days(reg)
    except NotFound:
        reg.close()
        return _err(404, "not_found", f"customer {customer_id!r} not found")
    except RegistryError as e:
        reg.close()
        return _err(503, "registry_unavailable", str(e))
    reg.close()
    return {
        "customer_id": customer_id,
        "retention_days": retention_days,
        "segments": [_segment_public(s) for s in rows],
    }


@router.get(
    "/customers/{customer_id}/recordings/{segment_id}",
    response_model=RecordingGetResponse,
    summary="Get one recording segment",
    description="Returns the segment if it belongs to this customer; otherwise 404.",
)
def get_customer_recording(customer_id: str, segment_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    seg, err = _load_segment_for_customer(segment_id, customer_id, reg)
    reg.close()
    if err is not None:
        return err
    assert seg is not None
    return {"segment": _segment_public(seg)}


@router.get(
    "/customers/{customer_id}/recordings/{segment_id}/playback",
    response_model=PresignResponse,
    summary="Presigned playback URL",
    description=(
        "Inline playback presign (no Content-Disposition attachment). "
        "Requires Platform S3/B2 credentials. TTL: `KALLON_S3_PRESIGN_TTL_SEC` (default 3600)."
    ),
)
def recording_playback(customer_id: str, segment_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    if not s3_storage.configured():
        return _err(503, "s3_not_configured", "platform S3 credentials not configured")
    reg = get_registry()
    seg, err = _load_segment_for_customer(segment_id, customer_id, reg)
    reg.close()
    if err is not None:
        return err
    assert seg is not None
    try:
        presigned = s3_storage.presign_get_object(bucket=seg.s3_bucket, key=seg.s3_key)
    except Exception as exc:  # noqa: BLE001
        log.exception("presign playback failed for %s", segment_id)
        return _err(502, "s3_error", str(exc))
    return {"segment_id": segment_id, **presigned}


@router.get(
    "/customers/{customer_id}/recordings/{segment_id}/download",
    response_model=PresignResponse,
    summary="Presigned download URL",
    description=(
        "Same as playback, but the presigned URL forces "
        "`Content-Disposition: attachment; filename=\"<segment.filename>\"`."
    ),
)
def recording_download(customer_id: str, segment_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    if not s3_storage.configured():
        return _err(503, "s3_not_configured", "platform S3 credentials not configured")
    reg = get_registry()
    seg, err = _load_segment_for_customer(segment_id, customer_id, reg)
    reg.close()
    if err is not None:
        return err
    assert seg is not None
    try:
        presigned = s3_storage.presign_get_object(
            bucket=seg.s3_bucket,
            key=seg.s3_key,
            download=True,
            filename=seg.filename,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("presign download failed for %s", segment_id)
        return _err(502, "s3_error", str(exc))
    return {"segment_id": segment_id, **presigned}


@router.delete(
    "/customers/{customer_id}/recordings/{segment_id}",
    response_model=DeleteResponse,
    summary="Delete recording segment",
    description=(
        "Deletes the S3 object when S3 is configured, then the registry row. "
        "If S3 delete fails, the registry row is kept (`502 s3_error`)."
    ),
)
def delete_recording(customer_id: str, segment_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    seg, err = _load_segment_for_customer(segment_id, customer_id, reg)
    if err is not None:
        reg.close()
        return err
    assert seg is not None
    if s3_storage.configured():
        try:
            s3_storage.delete_object(bucket=seg.s3_bucket, key=seg.s3_key)
        except Exception as exc:  # noqa: BLE001
            reg.close()
            log.exception("S3 delete failed for %s", segment_id)
            return _err(502, "s3_error", str(exc))
    try:
        reg.delete_recording_segment(segment_id)
        reg.audit("recording.delete", entity_id=segment_id, actor=customer_id)
    except RegistryError as e:
        reg.close()
        return _err(503, "registry_unavailable", str(e))
    reg.close()
    return {"status": "deleted", "segment_id": segment_id}


@router.get(
    "/platform/recording-retention",
    response_model=RetentionResponse,
    summary="Get cloud recording retention",
    description=(
        "Resolution: `KALLON_RECORDING_RETENTION_DAYS` env → "
        "`platform_config.recording_retention_days` → default `30`."
    ),
)
def get_recording_retention(request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        days = _retention_days(reg)
    finally:
        reg.close()
    return {"retention_days": days}


@router.put(
    "/platform/recording-retention",
    response_model=RetentionResponse,
    summary="Set cloud recording retention",
    description="Persists `retention_days` to `platform_config` (min 1).",
)
async def set_recording_retention(request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    try:
        payload = RetentionUpdateRequest.model_validate_json(await request.body() or b"{}")
    except ValidationError as e:
        return _err(422, "invalid_request", f"invalid request body: {e.errors()}")
    days = max(1, int(payload.retention_days))
    reg = get_registry()
    try:
        reg.set_platform_config("recording_retention_days", str(days))
    except RegistryError as e:
        reg.close()
        return _err(503, "registry_unavailable", str(e))
    reg.close()
    return {"retention_days": days}
