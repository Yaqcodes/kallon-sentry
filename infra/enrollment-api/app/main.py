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
import logging.handlers
import os
import re
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

# Make the repo-root `registry` package importable when run from infra/enrollment-api.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from registry import Conflict, NotFound, RegistryError, SubnetExhausted, get_registry  # noqa: E402
from registry.identity import validate  # noqa: E402

from . import peering  # noqa: E402
from .peering import get_peer_adder  # noqa: E402
from .alerts import router as alerts_router  # noqa: E402
from .platform import router as platform_router  # noqa: E402


def _bootstrap_env_file() -> None:
    """Load enrollment-api.env into os.environ so that file is the single
    source of truth regardless of how the process is launched.

    Why this exists: NSSM (the prescribed Windows service manager) injects env
    vars ONLY from its own AppEnvironmentExtra registry setting — it never reads
    enrollment-api.env. A bare terminal needs load-control-plane.ps1 dot-sourced
    in the same window. Both are easy to get subtly wrong, stranding a tower
    with a half-configured API. Loading the file here makes the file itself
    authoritative for every launch method.

    Precedence: an already-set environment variable WINS (the file only fills in
    what's missing), so an explicit NSSM/shell override is still honored.
    """
    default = (
        r"C:\kallon\config\enrollment-api.env" if os.name == "nt"
        else "/etc/kallon/enrollment-api.env"
    )
    env_file = os.environ.get("KALLON_ENROLLMENT_ENV_FILE", default)
    path = Path(env_file)
    if not path.is_file():
        return
    loaded = 0
    try:
        # utf-8-sig transparently strips a BOM if the file was saved from a
        # Windows editor / PowerShell Set-Content.
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            # Strip ONE pair of matching surrounding quotes only (leaves an
            # inner-quoted value like a command template intact).
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if not key or key in os.environ:
                continue
            os.environ[key] = val
            loaded += 1
    except OSError as exc:  # noqa: BLE001
        print(f"[enrollment] could not read {env_file}: {exc}", file=sys.stderr)
        return
    # Logging isn't configured yet — emit to stderr so it lands in NSSM's
    # AppStderr log and in a terminal.
    print(f"[enrollment] loaded {loaded} env var(s) from {env_file}", file=sys.stderr)


_bootstrap_env_file()


def _configure_logging() -> None:
    """Always write to a rotating file, regardless of how the process is
    launched (bare terminal, NSSM service, systemd — none of which reliably
    capture stdout without extra operator configuration). Falls back to
    console-only logging if the log directory isn't writable, so a logging
    problem can never take down the API itself.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = os.environ.get(
        "KALLON_ENROLLMENT_LOG_FILE",
        r"C:\kallon\logs\enrollment-api.log" if os.name == "nt" else "/var/log/kallon/enrollment-api.log",
    )
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        )
    except OSError as exc:  # noqa: BLE001
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("enrollment").warning(
            "could not open log file %s (%s); logging to console only", log_file, exc
        )
        return
    logging.basicConfig(
        level=os.environ.get("KALLON_ENROLLMENT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )
    logging.getLogger("enrollment").info("logging to %s", log_file)


_configure_logging()
log = logging.getLogger("enrollment")

OPENAPI_TAGS = [
    {
        "name": "Health",
        "description": "Liveness probe for load balancers and monitoring.",
    },
    {
        "name": "Enrollment",
        "description": "Tower first-boot enrollment. Called by Jetsons, not buyer dashboards.",
    },
    {
        "name": "Fleet",
        "description": "Customers and towers from the Postgres registry.",
    },
    {
        "name": "Tower proxy",
        "description": "PTZ, snapshots, sensors, and stream readiness proxied over WireGuard.",
    },
    {
        "name": "Alerts",
        "description": "Hub-forwarded tower events for customer dashboards (ingest, history, SSE).",
    },
]

app = FastAPI(
    title="Kallon Platform API",
    version="1.2",
    description=(
        "Unified Terra control plane: fleet registry, tower proxy, enrollment, and "
        "dashboard alert ingest. SDK consumers use `/v1/customers`, `/v1/towers`, and "
        "tower proxy routes. Towers use `/v1/enroll` on first boot."
    ),
    openapi_tags=OPENAPI_TAGS,
)


def _parse_cors_origins() -> list[str]:
    """Comma-separated browser origins allowed to call the Platform API cross-origin.

    Set ``KALLON_CORS_ORIGINS`` in enrollment-api.env, e.g.
    ``https://sentinel-dashboard.vercel.app,http://localhost:5174``. Use ``*`` only
    in lab — any website could then call the API from a user's browser session.
    """
    raw = os.environ.get("KALLON_CORS_ORIGINS", "").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]
    return [part.strip() for part in raw.split(",") if part.strip()]


_cors_origins = _parse_cors_origins()
if _cors_origins:
    log.info("CORS enabled for origins: %s", _cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    log.info("CORS disabled — set KALLON_CORS_ORIGINS for Vercel/browser dashboards")

# Fleet + tower-proxy + alerts (docs/platform-api.md). Enrollment routes below
# keep their original shapes — factory images depend on them.
app.include_router(platform_router)
app.include_router(alerts_router)


@app.on_event("startup")
def _startup_checks() -> None:
    # Never raises — logs everything an operator needs to fix a misconfigured
    # deploy before it strands a real tower. See peering.startup_check().
    peering.startup_check()


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch anything not already turned into an HTTPException (DB down,
    unexpected bugs, etc.) so a tower/operator never sees a bare, detail-less
    500. HTTPException/RequestValidationError still hit FastAPI's own more
    specific handlers first — this only fires for truly unexpected errors.
    """
    request_id = secrets.token_hex(4)
    log.exception("unhandled exception [%s] on %s %s", request_id, request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": (
                f"Unexpected server error (request_id={request_id}). "
                "Check enrollment-api.log for the full traceback at this request_id."
            ),
            "request_id": request_id,
        },
    )

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


# A WireGuard public key is base64 of 32 bytes: 43 base64 chars + one '='.
_WG_PUBKEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


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
@app.get("/healthz", tags=["Health"])
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/enroll", response_model=EnrollResponse, tags=["Enrollment"])
async def enroll(request: Request) -> EnrollResponse:
    await _verify_service_hmac(request)
    body = await request.body()
    try:
        payload = EnrollRequest.model_validate_json(body)
    except ValidationError as e:
        # Manually-parsed body (not a FastAPI signature param), so pydantic's
        # ValidationError would otherwise bubble up as a bare, detail-less 500.
        raise HTTPException(status_code=422, detail=f"invalid request body: {e.errors()}")

    # Reject a malformed WG key up front with a clear message, rather than
    # letting it reach the hub and surface as a confusing peer-add 502. A
    # corrupt key here almost always means the tower captured log noise into
    # its `--print-pubkey` output (fixed in kallon-wg-provision.sh).
    key = payload.wg_public_key.strip()
    if not _WG_PUBKEY_RE.match(key):
        preview = key[:60].replace("\n", "\\n").replace("\r", "\\r")
        raise HTTPException(
            status_code=422,
            detail=(
                "wg_public_key is not a valid WireGuard public key (expected 44 "
                f"base64 chars ending in '='). Got {len(key)} chars starting {preview!r}. "
                "On the tower, verify `kallon-wg-provision.sh --print-pubkey` emits "
                "only the key (update the tower if it prints log text too)."
            ),
        )
    payload.wg_public_key = key

    reg = _registry()
    try:
        # Resolve the tower by device_id or claim code (auto-enroll path).
        if payload.device_id:
            try:
                validate("device", payload.device_id)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=f"invalid device_id: {e}")
            try:
                tower = reg.get_tower(payload.device_id)
            except NotFound:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown device_id {payload.device_id!r} — not registered "
                    "in the registry (was register-tower run for it?)",
                )
        elif payload.claim_code:
            try:
                validate("claim", payload.claim_code)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=f"invalid claim_code: {e}")
            try:
                tower = reg.get_tower_by_claim(payload.claim_code)
            except NotFound:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown claim_code {payload.claim_code!r} — not registered "
                    "in the registry, or already redeemed",
                )
        else:
            raise HTTPException(
                status_code=422, detail="device_id or claim_code required (got neither)"
            )

        if tower.status == "suspended":
            raise HTTPException(
                status_code=403,
                detail=f"tower {tower.device_id} is suspended in the registry; "
                "reactivate it (registry.cli set-tower-status --status enrolled) before it can enroll",
            )

        # Per-tower token check.
        if not tower.enrollment_token_hash or not hmac.compare_digest(
            tower.enrollment_token_hash, _sha256(payload.enrollment_token)
        ):
            reg.audit("enroll_rejected", entity_id=tower.device_id, actor="enrollment-api",
                      payload_json={"reason": "bad_token"})
            raise HTTPException(
                status_code=401,
                detail=f"invalid enrollment token for {tower.device_id} — token mismatch "
                "against the registry hash (re-run register-tower if the token was lost)",
            )

        cust = reg.get_customer(tower.customer_id)
        if cust.status != "active":
            raise HTTPException(
                status_code=409,
                detail=f"customer {cust.customer_id} is not active (status={cust.status!r}); "
                "run `registry.cli set-hub --status active` once the hub is provisioned",
            )
        if not (cust.gateway_endpoint and cust.gateway_public_key):
            raise HTTPException(
                status_code=409,
                detail=f"customer {cust.customer_id} has no hub endpoint/pubkey configured; "
                "run `registry.cli set-hub --endpoint ... --pubkey ...` first",
            )

        # NOTE: intentionally NOT skipping add_peer just because the registry
        # already says enrolled/active with a matching key. The registry
        # cannot prove the hub is actually in sync (it wasn't, historically,
        # under KALLON_PEER_BACKEND=noop; it also wouldn't be after a hub
        # rebuild/config loss). kallon-gateway-add-peer.sh is idempotent
        # (`wg set` + rewrite), so re-asserting the peer on every enroll call
        # is cheap and makes the hub self-heal to match the registry with no
        # operator action, instead of trusting stale state forever.
        vpn_ip = tower.vpn_ip or reg.allocate_ip(tower.customer_id)
        try:
            get_peer_adder().add_peer(
                gateway_host=cust.gateway_endpoint.split(":")[0],
                pubkey=payload.wg_public_key,
                vpn_ip=vpn_ip,
                device_id=tower.device_id,
            )
        except RuntimeError:
            # Full traceback + subprocess stderr goes to the log file — this
            # is the "real error" to check first when a tower can't get a
            # WireGuard handshake. Deliberately NOT included in the response
            # body: it can contain hub-internal details (hostnames, script
            # paths) and this endpoint is reachable from the public internet.
            # IP is not yet persisted, so the tower's own retry loop will
            # safely re-attempt allocation + peer-add.
            request_id = secrets.token_hex(4)
            log.exception("peer-add failed [%s] for device=%s", request_id, tower.device_id)
            raise HTTPException(
                status_code=502,
                detail=(
                    f"hub peer-add failed for {tower.device_id} (request_id={request_id}) "
                    "— see enrollment-api.log for the full SSH/script output. The tower will "
                    "retry automatically."
                ),
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
    except HTTPException:
        raise
    except NotFound as e:
        # e.g. tower's customer_id row missing — shouldn't happen with intact
        # FKs, but readable beats a bare 500 if it ever does.
        raise HTTPException(status_code=404, detail=str(e))
    except Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SubnetExhausted as e:
        raise HTTPException(
            status_code=409,
            detail=f"no VPN IPs left for customer {tower.customer_id if 'tower' in locals() else '?'}: {e} "
            "— expand the customer's vpn_subnet or free unused allocations",
        )
    except RegistryError as e:
        # Any other registry error (e.g. DB connectivity) — still readable,
        # not a bare 500. Full traceback also goes to the log.
        log.exception("registry error during enroll for device_id=%s", payload.device_id)
        raise HTTPException(status_code=503, detail=f"registry unavailable, try again: {e}")
    finally:
        reg.close()


@app.post("/v1/enroll/confirm", tags=["Enrollment"])
async def enroll_confirm(req: ConfirmRequest) -> dict:
    stored = _CONFIRM_TOKENS.get(req.device_id)
    if not stored or not hmac.compare_digest(stored, _sha256(req.confirm_token)):
        raise HTTPException(
            status_code=401,
            detail=f"invalid or expired confirm_token for {req.device_id} — confirm tokens are "
            "one-time and issued by /v1/enroll; re-run /v1/enroll to get a fresh one",
        )
    if not req.handshake_ok:
        raise HTTPException(
            status_code=400,
            detail=(
                f"handshake not ok for {req.device_id}; not activating. The tower did not see a "
                "live WireGuard handshake within its wait window. Check enrollment-api.log for "
                f"the add_peer result for {req.device_id} — if that succeeded, verify UDP 51820 "
                "is reachable from the tower to the hub endpoint and that the tower's wg0.conf "
                "endpoint/AllowedIPs are correct."
            ),
        )

    reg = _registry()
    try:
        reg.set_tower_status(req.device_id, "active")
        reg.audit("tower_active", entity_id=req.device_id, actor="enrollment-api")
    except NotFound:
        raise HTTPException(
            status_code=404,
            detail=f"unknown device_id {req.device_id!r} in confirm — was it registered?",
        )
    finally:
        reg.close()

    _CONFIRM_TOKENS.pop(req.device_id, None)  # one-time
    return {"device_id": req.device_id, "status": "active"}
