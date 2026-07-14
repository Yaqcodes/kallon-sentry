"""Kallon Platform API router — fleet + tower proxy endpoints.

The SDK-facing surface documented in docs/platform-api.md and consumed by
sentinel-sdk (https://github.com/Yaqcodes/sentinel-sdk). Included by app.main.

Two endpoint families:

  Fleet  — customers/towers straight from the registry (Postgres).
  Proxy  — PTZ / snapshot / status / streams forwarded over WireGuard to the
           tower gateway (infra/tower-dashboard/gateway.py) at
           http://<tower-vpn-ip>:8766. SDK consumers never call a tower
           directly; the control plane is the single base URL.

Error contract (platform endpoints only — enrollment keeps FastAPI "detail"):

  {"error": {"code": "...", "message": "...", ...context}}

  404 not_found | 409 tower_not_enrolled | 422 invalid_request
  502 tower_error | 503 tower_offline | 503 registry_unavailable

Auth: NOT ENFORCED YET (recorded decision — planning/sdk-implementation-plan.md
§5.1). If KALLON_PLATFORM_API_KEY is set, X-Kallon-Api-Key is required; when
unset all requests pass. Do not expose these routes publicly until enforced.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from registry import Conflict, NotFound, RegistryError, get_registry
from registry.identity import device_id as make_device_id
from registry.identity import new_claim_code, new_enrollment_token, slug_of
from registry.interface import Customer, Tower

log = logging.getLogger("platform")

router = APIRouter(prefix="/v1")

TOWER_GATEWAY_PORT = int(os.environ.get("KALLON_TOWER_GATEWAY_PORT", "8766"))
HUB_PROXY_PORT = int(os.environ.get("KALLON_HUB_PROXY_PORT", "8767"))
HUB_PROXY_TOKEN = os.environ.get("KALLON_HUB_PROXY_TOKEN", "")
PROXY_CONNECT_TIMEOUT = float(os.environ.get("KALLON_PROXY_CONNECT_TIMEOUT", "3"))
PROXY_READ_TIMEOUT = float(os.environ.get("KALLON_PROXY_READ_TIMEOUT", "10"))
SNAPSHOT_READ_TIMEOUT = float(os.environ.get("KALLON_SNAPSHOT_READ_TIMEOUT", "20"))
PLATFORM_API_KEY = os.environ.get("KALLON_PLATFORM_API_KEY", "")

# When true (default), Artemis dials the customer hub tower-proxy on the public
# internet instead of the tower VPN IP. Set KALLON_PROXY_VIA_HUB=0 only for
# lab setups where Artemis is itself a WireGuard NOC peer.
PROXY_VIA_HUB = os.environ.get("KALLON_PROXY_VIA_HUB", "1").strip().lower() not in (
    "0", "false", "no", "off",
)


# ── error envelope ───────────────────────────────────────────────────────────
def _err(status: int, code: str, message: str, **context: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **context}})


def _auth_check(request: Request) -> Optional[JSONResponse]:
    """Soft auth gate. Enforced only when KALLON_PLATFORM_API_KEY is set —
    see the auth decision in planning/sdk-implementation-plan.md §5.1."""
    if not PLATFORM_API_KEY:
        return None
    provided = request.headers.get("X-Kallon-Api-Key", "")
    if provided != PLATFORM_API_KEY:
        return _err(401, "unauthorized", "missing or invalid X-Kallon-Api-Key")
    return None


# ── serialization (public fields only — never secrets) ──────────────────────
def _customer_public(c: Customer) -> dict[str, Any]:
    return {
        "customer_id": c.customer_id,
        "display_name": c.display_name,
        "vpn_subnet": c.vpn_subnet,
        "gateway_id": c.gateway_id,
        "gateway_endpoint": c.gateway_endpoint,
        "gateway_public_key": c.gateway_public_key,
        "hub_alert_url": c.hub_alert_url,
        "hub_provider": c.hub_provider,
        "hub_host_id": c.hub_host_id,
        "status": c.status,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _tower_public(t: Tower) -> dict[str, Any]:
    # claim_code and enrollment_token_hash are intentionally excluded.
    return {
        "device_id": t.device_id,
        "customer_id": t.customer_id,
        "group_id": t.group_id,
        "vpn_ip": t.vpn_ip,
        "wg_public_key": t.wg_public_key,
        "status": t.status,
        "acceptance_status": t.acceptance_status,
        "manufactured_at": t.manufactured_at.isoformat() if t.manufactured_at else None,
        "enrolled_at": t.enrolled_at.isoformat() if t.enrolled_at else None,
        "shipped_at": t.shipped_at.isoformat() if t.shipped_at else None,
        "rtsp_base": f"rtsp://{t.vpn_ip}:8554" if t.vpn_ip else None,
    }


# ── fleet endpoints ──────────────────────────────────────────────────────────
@router.get("/customers", tags=["Fleet"])
def list_customers(request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        return {"customers": [_customer_public(c) for c in reg.list_customers()]}
    except RegistryError as e:
        log.exception("registry error in list_customers")
        return _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


@router.get("/customers/{customer_id}", tags=["Fleet"])
def get_customer(customer_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        return _customer_public(reg.get_customer(customer_id))
    except NotFound:
        return _err(404, "not_found", f"unknown customer {customer_id!r}")
    except RegistryError as e:
        log.exception("registry error in get_customer")
        return _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


@router.get("/customers/{customer_id}/towers", tags=["Fleet"])
def list_customer_towers(customer_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        reg.get_customer(customer_id)  # 404 for unknown customer
        return {"towers": [_tower_public(t) for t in reg.list_towers(customer_id)]}
    except NotFound:
        return _err(404, "not_found", f"unknown customer {customer_id!r}")
    except RegistryError as e:
        log.exception("registry error in list_customer_towers")
        return _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


@router.get("/towers", tags=["Fleet"])
def list_towers(request: Request, status: Optional[str] = None):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        towers = [_tower_public(t) for t in reg.list_towers()]
        if status:
            towers = [t for t in towers if t["status"] == status]
        return {"towers": towers}
    except RegistryError as e:
        log.exception("registry error in list_towers")
        return _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


@router.get("/towers/{device_id}", tags=["Fleet"])
def get_tower(device_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        return _tower_public(reg.get_tower(device_id))
    except NotFound:
        return _err(404, "not_found", f"unknown device_id {device_id!r}")
    except RegistryError as e:
        log.exception("registry error in get_tower")
        return _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


class RegisterTowerRequest(BaseModel):
    customer_id: str
    serial: int = Field(ge=0, le=999999)
    group_id: Optional[str] = None


@router.post("/towers", status_code=201, tags=["Fleet"])
async def register_tower(request: Request):
    """Factory registration. Terra-ops-only until auth lands (returns a
    one-time enrollment token — see docs/platform-api.md §2)."""
    if (resp := _auth_check(request)) is not None:
        return resp
    try:
        payload = RegisterTowerRequest.model_validate_json(await request.body())
    except ValidationError as e:
        return _err(422, "invalid_request", f"invalid request body: {e.errors()}")

    reg = get_registry()
    try:
        cust = reg.get_customer(payload.customer_id)
        did = make_device_id(slug_of(cust.customer_id), payload.serial)
        token = new_enrollment_token()
        claim = new_claim_code()
        reg.register_tower(Tower(
            device_id=did,
            customer_id=cust.customer_id,
            group_id=payload.group_id,
            claim_code=claim,
            enrollment_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        ))
        reg.audit("tower_registered", entity_id=did, actor="platform-api")
        return JSONResponse(status_code=201, content={
            "device_id": did,
            "customer_id": cust.customer_id,
            "claim_code": claim,
            "enrollment_token": token,  # plaintext shown once; registry stores hash
        })
    except NotFound:
        return _err(404, "not_found", f"unknown customer {payload.customer_id!r}")
    except Conflict as e:
        return _err(409, "conflict", str(e))
    except ValueError as e:
        return _err(422, "invalid_request", str(e))
    except RegistryError as e:
        log.exception("registry error in register_tower")
        return _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


# ── tower proxy ──────────────────────────────────────────────────────────────
def _hub_host_from_endpoint(gateway_endpoint: Optional[str]) -> Optional[str]:
    """Extract public host from registry gateway_endpoint (host:51820)."""
    if not gateway_endpoint:
        return None
    host = gateway_endpoint.strip().split(":", 1)[0].strip()
    return host or None


def _tower_proxy_target(
    device_id: str,
) -> tuple[Optional[str], Optional[str], Optional[JSONResponse]]:
    """Resolve Artemisper → hub/tower URL pieces.

    Returns (request_url_base_with_path_prefix, vpn_ip, error).
    When PROXY_VIA_HUB: base is ``http://{hub}:{port}/proxy/{device_id}``.
    When direct (lab): base is ``http://{vpn_ip}:{tower_port}``.
    Paths appended by callers are tower-gateway paths (``/api/...``).
    """
    reg = get_registry()
    try:
        tower = reg.get_tower(device_id)
        if not tower.vpn_ip:
            return None, None, _err(
                409, "tower_not_enrolled",
                f"tower {device_id} has no VPN IP yet (status={tower.status!r}); "
                "it must complete first-boot enrollment before tower APIs work",
                device_id=device_id,
            )
        if not PROXY_VIA_HUB:
            return f"http://{tower.vpn_ip}:{TOWER_GATEWAY_PORT}", tower.vpn_ip, None

        try:
            cust = reg.get_customer(tower.customer_id)
        except NotFound:
            return None, None, _err(
                404, "not_found",
                f"customer {tower.customer_id!r} missing for tower {device_id}",
                device_id=device_id,
            )
        hub_host = _hub_host_from_endpoint(cust.gateway_endpoint)
        if not hub_host:
            return None, None, _err(
                503, "hub_unreachable",
                f"customer {tower.customer_id} has no gateway_endpoint; "
                "provision the hub before tower proxy works",
                device_id=device_id,
            )
        if not HUB_PROXY_TOKEN:
            return None, None, _err(
                503, "hub_proxy_misconfigured",
                "KALLON_HUB_PROXY_TOKEN is unset on the control plane",
                device_id=device_id,
            )
        base = f"http://{hub_host}:{HUB_PROXY_PORT}/proxy/{device_id}"
        return base, tower.vpn_ip, None
    except NotFound:
        return None, None, _err(404, "not_found", f"unknown device_id {device_id!r}")
    except RegistryError as e:
        log.exception("registry error resolving tower %s", device_id)
        return None, None, _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


async def _proxy(
    device_id: str,
    method: str,
    path: str,
    *,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
    read_timeout: float = PROXY_READ_TIMEOUT,
) -> Response:
    base, vpn_ip, err = _tower_proxy_target(device_id)
    if err is not None:
        return err
    assert base is not None and vpn_ip is not None
    timeout = httpx.Timeout(connect=PROXY_CONNECT_TIMEOUT, read=read_timeout, write=10.0, pool=5.0)
    headers: dict[str, str] = {}
    if PROXY_VIA_HUB:
        headers["X-Kallon-Hub-Proxy-Token"] = HUB_PROXY_TOKEN
        headers["X-Kallon-Tower-Vpn-Ip"] = vpn_ip
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, f"{base}{path}", json=json_body, params=params, headers=headers,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as exc:
        log.warning("tower %s unreachable at %s%s: %s", device_id, base, path, exc)
        return _err(
            503, "tower_offline",
            f"tower did not respond ({exc.__class__.__name__}) — "
            + ("hub proxy unreachable or VPN tunnel down" if PROXY_VIA_HUB
               else "VPN tunnel down or tower rebooting"),
            device_id=device_id,
        )
    # Hub agent already returns platform-shaped errors; pass through.
    content_type = resp.headers.get("content-type", "application/json")
    return Response(content=resp.content, status_code=resp.status_code, media_type=content_type)


class PTZMoveRequest(BaseModel):
    camera: int = 1
    mode: str = "absolute"
    pan: Optional[float] = None
    tilt: Optional[float] = None
    zoom: Optional[float] = None
    seconds: Optional[float] = Field(default=None, le=10)


@router.post("/towers/{device_id}/ptz/move", tags=["Tower proxy"])
async def ptz_move(device_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    try:
        payload = PTZMoveRequest.model_validate_json(await request.body())
    except ValidationError as e:
        return _err(422, "invalid_request", f"invalid request body: {e.errors()}")
    # Fail fast at the control plane — don't spend a VPN round trip on a
    # request the tower gateway would reject anyway.
    if payload.mode not in ("absolute", "continuous"):
        return _err(422, "invalid_request", f"unknown mode {payload.mode!r} (absolute|continuous)")
    required = ("pan", "tilt") if payload.mode == "absolute" else ("pan", "tilt", "zoom", "seconds")
    missing = [k for k in required if getattr(payload, k) is None]
    if missing:
        return _err(422, "invalid_request", f"missing fields for {payload.mode} move: {missing}")
    body = {k: v for k, v in payload.model_dump().items() if v is not None}
    return await _proxy(device_id, "POST", "/api/ptz/move", json_body=body)


class PTZStopRequest(BaseModel):
    camera: int = 1
    home: bool = False


@router.post("/towers/{device_id}/ptz/stop", tags=["Tower proxy"])
async def ptz_stop(device_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    try:
        payload = PTZStopRequest.model_validate_json(await request.body() or b"{}")
    except ValidationError as e:
        return _err(422, "invalid_request", f"invalid request body: {e.errors()}")
    return await _proxy(device_id, "POST", "/api/ptz/stop", json_body=payload.model_dump())


@router.get("/towers/{device_id}/ptz/status", tags=["Tower proxy"])
async def ptz_status(device_id: str, request: Request, camera: int = 1):
    if (resp := _auth_check(request)) is not None:
        return resp
    return await _proxy(device_id, "GET", "/api/ptz/status", params={"camera": camera})


@router.get("/towers/{device_id}/snapshot/cam{camera}", tags=["Tower proxy"])
async def snapshot(device_id: str, camera: int, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    return await _proxy(
        device_id, "GET", f"/api/snapshot/cam{camera}", read_timeout=SNAPSHOT_READ_TIMEOUT
    )


@router.get("/towers/{device_id}/status", tags=["Tower proxy"])
async def tower_status(device_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    return await _proxy(device_id, "GET", "/api/status")


@router.get("/towers/{device_id}/streams", tags=["Tower proxy"])
async def tower_streams(device_id: str, request: Request):
    if (resp := _auth_check(request)) is not None:
        return resp
    return await _proxy(device_id, "GET", "/api/streams")
