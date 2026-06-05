"""Kallon enrollment API.

The only tower-facing interface to the registry. Towers auto-enroll on first
boot over HTTPS (TLS terminated by a reverse proxy — see deploy notes). The API
never exposes Postgres directly.

Auth (defense in depth):
  1. Per-tower one-time enrollment token (sha256 compared to the registry).
  2. Optional service-level HMAC over the raw body (ENROLLMENT_HMAC_KEY) proving
     the caller runs a genuine Kallon factory image.

Endpoints:
  GET  /healthz
  POST /v1/enroll          { device_id?, claim_code?, wg_public_key, enrollment_token }
  POST /v1/enroll/confirm  { device_id, confirm_token, handshake_ok }
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# Make the repo-root `registry` package importable when run from infra/enrollment-api.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from registry import Conflict, NotFound, get_registry  # noqa: E402
from registry.identity import validate  # noqa: E402

from .peering import get_peer_adder  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("enrollment")

app = FastAPI(title="Kallon Enrollment API", version="1.0")

# In-memory confirm-token store (device_id -> sha256 hex). Single-process v1;
# move to a short-TTL table/redis if the API is scaled horizontally.
_CONFIRM_TOKENS: dict[str, str] = {}

ENROLLMENT_HMAC_KEY = os.environ.get("ENROLLMENT_HMAC_KEY", "")


# ── models ───────────────────────────────────────────────────────────────────
class EnrollRequest(BaseModel):
    wg_public_key: str = Field(min_length=8)
    enrollment_token: str
    device_id: str | None = None
    claim_code: str | None = None


class EnrollResponse(BaseModel):
    device_id: str
    vpn_ip: str
    vpn_subnet: str
    gateway_endpoint: str
    gateway_public_key: str
    alert_webhook_url: str
    confirm_token: str


class ConfirmRequest(BaseModel):
    device_id: str
    confirm_token: str
    handshake_ok: bool = True


# ── helpers ──────────────────────────────────────────────────────────────────
def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def _verify_service_hmac(request: Request) -> bytes:
    body = await request.body()
    if ENROLLMENT_HMAC_KEY:
        sig = request.headers.get("X-Kallon-Enroll-Signature", "")
        expected = hmac.new(ENROLLMENT_HMAC_KEY.encode(), body, hashlib.sha256).hexdigest()
        provided = sig.removeprefix("sha256=")
        if not hmac.compare_digest(expected, provided):
            raise HTTPException(status_code=401, detail="invalid service signature")
    return body


def _registry():
    # New connection per request keeps the API stateless w.r.t. the DB.
    return get_registry()


# ── routes ───────────────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/enroll", response_model=EnrollResponse)
async def enroll(request: Request) -> EnrollResponse:
    await _verify_service_hmac(request)
    payload = EnrollRequest.model_validate_json(await request.body())

    reg = _registry()
    try:
        # Resolve the tower by device_id or claim code (auto-enroll path).
        if payload.device_id:
            validate("device", payload.device_id)
            try:
                tower = reg.get_tower(payload.device_id)
            except NotFound:
                raise HTTPException(status_code=404, detail="unknown device_id")
        elif payload.claim_code:
            validate("claim", payload.claim_code)
            try:
                tower = reg.get_tower_by_claim(payload.claim_code)
            except NotFound:
                raise HTTPException(status_code=404, detail="unknown claim_code")
        else:
            raise HTTPException(status_code=422, detail="device_id or claim_code required")

        # Per-tower token check.
        if not tower.enrollment_token_hash or not hmac.compare_digest(
            tower.enrollment_token_hash, _sha256(payload.enrollment_token)
        ):
            reg.audit("enroll_rejected", entity_id=tower.device_id, actor="enrollment-api",
                      payload_json={"reason": "bad_token"})
            raise HTTPException(status_code=401, detail="invalid enrollment token")

        cust = reg.get_customer(tower.customer_id)
        if cust.status != "active" or not (cust.gateway_endpoint and cust.gateway_public_key):
            raise HTTPException(status_code=409, detail="customer hub not provisioned yet")

        # Idempotency: re-enroll with the same key returns the existing config.
        if tower.status in ("enrolled", "active") and tower.wg_public_key == payload.wg_public_key \
                and tower.vpn_ip:
            vpn_ip = tower.vpn_ip
        else:
            vpn_ip = tower.vpn_ip or reg.allocate_ip(tower.customer_id)
            get_peer_adder().add_peer(
                gateway_host=cust.gateway_endpoint.split(":")[0],
                pubkey=payload.wg_public_key,
                vpn_ip=vpn_ip,
                device_id=tower.device_id,
            )
            reg.mark_tower_enrolled(tower.device_id, wg_public_key=payload.wg_public_key, vpn_ip=vpn_ip)
            reg.audit("tower_enrolled", entity_id=tower.device_id, actor="enrollment-api",
                      payload_json={"vpn_ip": vpn_ip})

        confirm_token = "cnf_" + secrets.token_urlsafe(24)
        _CONFIRM_TOKENS[tower.device_id] = _sha256(confirm_token)

        return EnrollResponse(
            device_id=tower.device_id,
            vpn_ip=vpn_ip,
            vpn_subnet=cust.vpn_subnet,
            gateway_endpoint=cust.gateway_endpoint,
            gateway_public_key=cust.gateway_public_key,
            alert_webhook_url=cust.hub_alert_url or f"http://{vpn_ip.rsplit('.',1)[0]}.1:8080/alerts",
            confirm_token=confirm_token,
        )
    except Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        reg.close()


@app.post("/v1/enroll/confirm")
async def enroll_confirm(req: ConfirmRequest) -> dict:
    stored = _CONFIRM_TOKENS.get(req.device_id)
    if not stored or not hmac.compare_digest(stored, _sha256(req.confirm_token)):
        raise HTTPException(status_code=401, detail="invalid confirm token")
    if not req.handshake_ok:
        raise HTTPException(status_code=400, detail="handshake not ok; not activating")

    reg = _registry()
    try:
        reg.set_tower_status(req.device_id, "active")
        reg.audit("tower_active", entity_id=req.device_id, actor="enrollment-api")
    except NotFound:
        raise HTTPException(status_code=404, detail="unknown device_id")
    finally:
        reg.close()

    _CONFIRM_TOKENS.pop(req.device_id, None)  # one-time
    return {"device_id": req.device_id, "status": "active"}
