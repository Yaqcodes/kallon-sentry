"""Customer dashboard alert ingest + query + SSE fan-out.

Hub listeners forward verified tower alerts to POST /v1/alerts/ingest
(set ALERT_FORWARD_URL on each hub). The dashboard consumes recent history
via GET /v1/alerts or live updates via GET /v1/events (SSE).

In-memory v1 store — sufficient for a single Artemis control-plane process.
Move to Redis/Postgres if the API is scaled horizontally.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from registry import NotFound, RegistryError, get_registry

from .platform import _auth_check, _err

log = logging.getLogger("alerts")

router = APIRouter(prefix="/v1", tags=["Alerts"])

ALERT_HISTORY_MAX = int(os.environ.get("KALLON_ALERT_HISTORY_MAX", "500"))
ALERT_INGEST_TOKEN = os.environ.get("KALLON_ALERT_INGEST_TOKEN", "")
_SEVERITY_ALIASES = {"info": "info", "warning": "warning", "critical": "critical"}


def normalize_alert(raw: dict[str, Any], *, customer_id: Optional[str] = None) -> dict[str, Any]:
    """Coerce a watchdog/hub alert into a stable dashboard shape."""
    alert_type = str(raw.get("alert_type") or raw.get("type") or "UNKNOWN")
    severity = str(raw.get("severity") or "").lower() or "info"
    severity = _SEVERITY_ALIASES.get(severity, severity)
    return {
        "device_id": raw.get("device_id"),
        "customer_id": customer_id,
        "alert_type": alert_type,
        "kind": alert_type.lower(),
        "severity": severity,
        "timestamp_utc": raw.get("timestamp_utc"),
        "nonce": raw.get("nonce"),
        "details": raw.get("details") or {},
        "received_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class AlertBus:
    """Thread-safe ring buffer + pub/sub for SSE subscribers."""

    def __init__(self, history: int) -> None:
        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = []
        self._max = history
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._seen: set[tuple[Any, ...]] = set()

    def publish(self, alert: dict[str, Any]) -> bool:
        key = (
            alert.get("device_id"),
            alert.get("alert_type"),
            alert.get("timestamp_utc"),
            alert.get("nonce"),
        )
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > self._max * 4:
                self._seen = set(list(self._seen)[-self._max * 2 :])
            self._history.append(alert)
            if len(self._history) > self._max:
                self._history = self._history[-self._max :]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(alert)
            except queue.Full:  # pragma: no cover
                pass
        return True

    def recent(
        self,
        *,
        customer_id: Optional[str] = None,
        device_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._history)
        if customer_id:
            rows = [a for a in rows if a.get("customer_id") == customer_id]
        if device_id:
            rows = [a for a in rows if a.get("device_id") == device_id]
        limit = max(1, min(limit, ALERT_HISTORY_MAX))
        return list(reversed(rows[-limit:]))

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(q)


BUS = AlertBus(ALERT_HISTORY_MAX)


def _ingest_auth(request: Request) -> Optional[JSONResponse]:
    if not ALERT_INGEST_TOKEN:
        return None
    provided = request.headers.get("X-Kallon-Ingest-Token", "")
    if provided != ALERT_INGEST_TOKEN:
        return _err(401, "unauthorized", "missing or invalid X-Kallon-Ingest-Token")
    return None


def _resolve_customer(device_id: Optional[str]) -> tuple[Optional[str], Optional[JSONResponse]]:
    if not device_id:
        return None, _err(422, "invalid_request", "alert body must include device_id")
    reg = get_registry()
    try:
        tower = reg.get_tower(str(device_id))
        return tower.customer_id, None
    except NotFound:
        return None, _err(404, "not_found", f"unknown device_id {device_id!r}")
    except RegistryError as e:
        log.exception("registry error resolving device_id %s", device_id)
        return None, _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()


@router.post("/alerts/ingest", status_code=201)
async def ingest_alert(request: Request):
    """Receive a hub-forwarded alert (ALERT_FORWARD_URL target).

    Optional gate: set KALLON_ALERT_INGEST_TOKEN and send it as
    X-Kallon-Ingest-Token from the hub forwarder.
    """
    if (resp := _ingest_auth(request)) is not None:
        return resp
    try:
        raw = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return _err(422, "invalid_request", "body is not valid JSON")
    if not isinstance(raw, dict):
        return _err(422, "invalid_request", "body must be a JSON object")

    customer_id, err = _resolve_customer(raw.get("device_id"))
    if err is not None:
        return err

    alert = normalize_alert(raw, customer_id=customer_id)
    accepted = BUS.publish(alert)
    log.info(
        "alert ingest device=%s type=%s customer=%s dedup=%s",
        alert.get("device_id"),
        alert.get("alert_type"),
        customer_id,
        not accepted,
    )
    return JSONResponse(
        status_code=201 if accepted else 200,
        content={"status": "accepted" if accepted else "duplicate", "alert": alert},
    )


@router.get("/alerts")
def list_alerts(
    request: Request,
    customer_id: Optional[str] = None,
    device_id: Optional[str] = None,
    limit: int = 100,
):
    if (resp := _auth_check(request)) is not None:
        return resp
    return {
        "alerts": BUS.recent(customer_id=customer_id, device_id=device_id, limit=limit),
    }


@router.get("/customers/{customer_id}/alerts")
def list_customer_alerts(customer_id: str, request: Request, limit: int = 100):
    if (resp := _auth_check(request)) is not None:
        return resp
    reg = get_registry()
    try:
        reg.get_customer(customer_id)
    except NotFound:
        return _err(404, "not_found", f"unknown customer {customer_id!r}")
    except RegistryError as e:
        log.exception("registry error in list_customer_alerts")
        return _err(503, "registry_unavailable", str(e))
    finally:
        reg.close()
    return {"alerts": BUS.recent(customer_id=customer_id, limit=limit)}


@router.get("/events")
async def alert_events(request: Request, customer_id: Optional[str] = None):
    """Server-Sent Events stream of ingested alerts.

    Optional ?customer_id= filter for multi-tenant dashboards.
    """
    if (resp := _auth_check(request)) is not None:
        return resp

    async def stream():
        q = BUS.subscribe()
        try:
            # Replay recent history so a new subscriber sees context immediately.
            for alert in reversed(BUS.recent(customer_id=customer_id, limit=50)):
                if customer_id is None or alert.get("customer_id") == customer_id:
                    yield f"data: {json.dumps(alert, separators=(',', ':'))}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    alert = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: q.get(timeout=25.0)
                    )
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if customer_id is not None and alert.get("customer_id") != customer_id:
                    continue
                yield f"data: {json.dumps(alert, separators=(',', ':'))}\n\n"
        finally:
            BUS.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
