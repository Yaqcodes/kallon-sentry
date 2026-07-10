# Kallon Platform API — Contract (v1)

**Terra Industries · Internal Engineering · July 2026**

The unified, SDK-facing HTTP API for the Kallon platform. It is served by the
control plane service (`infra/enrollment-api/`, FastAPI) and consists of:

- **Fleet endpoints** — customers/towers, backed directly by the Postgres registry.
- **Tower proxy endpoints** — PTZ, snapshots, sensor status, stream readiness;
  forwarded by the control plane over WireGuard to the tower gateway
  (`infra/tower-dashboard/gateway.py`, port `8766` on the tower's VPN IP).
- **Enrollment endpoints** — pre-existing first-boot flow (unchanged).

SDK consumers use **one base URL** (the control plane). They never call a
tower directly. The client library for this API is
[`sentinel-sdk`](https://github.com/Yaqcodes/sentinel-sdk).

Machine-readable spec: `GET /openapi.json` on a running control plane.

> **Auth status (July 2026):** no authentication is enforced yet — see
> `planning/sdk-implementation-plan.md` §5.1. Clients SHOULD already send
> `X-Kallon-Api-Key` so they need no changes when enforcement lands. Until
> then, do not expose fleet/proxy routes beyond the ops network; only
> `/v1/enroll*` may be public.

---

## 1. Conventions

- Base path: `/v1`. Breaking changes bump the path version.
- Bodies: JSON (`Content-Type: application/json`), except snapshots
  (`image/jpeg`).
- Identifiers follow `docs/identity-and-secrets.md`
  (`cust_<slug>`, `kln_<slug>_<6 digits>`).

### 1.1 Error envelope

All platform endpoints (fleet + proxy) return errors as:

```json
{"error": {"code": "<machine_code>", "message": "<human readable>", "...": "context"}}
```

| HTTP | code | Meaning |
|------|------|---------|
| 404 | `not_found` | Unknown customer/tower/camera |
| 409 | `tower_not_enrolled` | Tower registered but has no VPN IP yet |
| 422 | `invalid_request` | Malformed body/params |
| 502 | `tower_error` | Tower reached, but its gateway returned an error |
| 503 | `tower_offline` | Tower unreachable over VPN (tunnel down / rebooting) |
| 503 | `registry_unavailable` | Registry DB unreachable |

`tower_offline` example:

```json
{"error": {"code": "tower_offline",
           "message": "tower did not respond within 10.0s",
           "device_id": "kln_acme_000042"}}
```

The pre-existing enrollment endpoints keep FastAPI's `{"detail": ...}` error
shape — that contract already ships in factory images and is not changed.

### 1.2 Proxy latency

Proxied calls add one VPN round trip (typically 50–100 ms) plus camera ONVIF
latency for PTZ (Dahua ceiling ~1.6 s p95). Snapshot capture takes 1–4 s
(ffmpeg connects to the local RTSP stream and decodes one frame).

---

## 2. Fleet endpoints

### GET /v1/customers

List all customer orgs.

```json
{"customers": [
  {"customer_id": "cust_acme", "display_name": "Acme Security",
   "vpn_subnet": "10.50.0.0/24", "gateway_endpoint": "203.0.113.42:51820",
   "hub_alert_url": "http://10.50.0.1:8080/alerts", "hub_provider": "lightsail",
   "status": "active", "created_at": "2026-06-01T10:00:00Z"}
]}
```

`gateway_public_key`, `gateway_id`, `hub_host_id` are included; WireGuard
private keys and HMAC keys are never returned by any endpoint.

### GET /v1/customers/{customer_id}

Single customer object (same shape). `404 not_found` if unknown.

### GET /v1/customers/{customer_id}/towers

```json
{"towers": [
  {"device_id": "kln_acme_000042", "customer_id": "cust_acme",
   "group_id": null, "vpn_ip": "10.50.0.2", "wg_public_key": "…=",
   "status": "active", "acceptance_status": "pending",
   "manufactured_at": "2026-06-10T09:00:00Z",
   "enrolled_at": "2026-06-20T07:12:44Z", "shipped_at": null,
   "rtsp_base": "rtsp://10.50.0.2:8554"}
]}
```

`rtsp_base` is derived (null until enrolled). Camera paths are `cam1..camN`.
`claim_code` and `enrollment_token_hash` are **not** returned.

### GET /v1/towers

All towers across customers (Terra ops). Optional `?status=` filter.

### GET /v1/towers/{device_id}

Single tower object. `404 not_found` if unknown.

### POST /v1/towers

Factory registration (**Terra-ops-only** until auth lands — returns a
one-time secret).

Request:

```json
{"customer_id": "cust_acme", "serial": 43, "group_id": null}
```

Response `201`:

```json
{"device_id": "kln_acme_000043", "claim_code": "clm_…",
 "enrollment_token": "enr_…", "customer_id": "cust_acme"}
```

The `enrollment_token` plaintext is shown **once**; the registry stores only
its SHA-256. `409` if the device_id already exists; `404` if the customer is
unknown.

---

## 3. Tower proxy endpoints

All are `/v1/towers/{device_id}/…`. Common failure modes: `409
tower_not_enrolled`, `503 tower_offline`, `502 tower_error`.

### POST /v1/towers/{device_id}/ptz/move

Absolute or continuous PTZ move.

```json
// absolute — pan/tilt in [-1,1], zoom in [0,1] (ONVIF normalized space)
{"camera": 1, "mode": "absolute", "pan": 0.5, "tilt": -0.2, "zoom": 0.0}

// continuous — velocities in [-1,1], seconds ≤ 10
{"camera": 1, "mode": "continuous", "pan": 0.3, "tilt": 0.0, "zoom": 0.0, "seconds": 0.5}
```

`camera` defaults to 1. Response mirrors the PTZ daemon result:

```json
{"ok": true, "result": {"ok": true, "round_trip_ms": 1240.5}}
```

### POST /v1/towers/{device_id}/ptz/stop

`{"camera": 1}` → `{"ok": true, "result": {}}`. Also `"home"` supported via
`{"camera": 1, "home": true}`.

### GET /v1/towers/{device_id}/ptz/status?camera=1

```json
{"ok": true, "result": {"pan": 0.5, "tilt": -0.2, "zoom": 0.0}}
```

### GET /v1/towers/{device_id}/snapshot/cam{n}

Returns `200` with `Content-Type: image/jpeg` — a single still frame captured
on the tower from `rtsp://127.0.0.1:8554/cam<n>`. `404 not_found` if camera
index is out of range for the tower.

### GET /v1/towers/{device_id}/status

Watchdog sensor/health snapshot, proxied from the tower's status API:

```json
{"available": true, "device_id": "kln_acme_000042",
 "sensors": {"mpu6050": {...}, "reed": {...}, "ldr": {...}},
 "streams": {...}, "temperature_c": 46.2, "...": "…"}
```

Shape is the watchdog's `StatusStore.snapshot()`; keys vary with enabled
hardware. `available: false` (HTTP 200) when the tower is reachable but its
watchdog status API is not running.

### GET /v1/towers/{device_id}/streams

mediamtx path readiness:

```json
{"available": true, "paths": [
  {"name": "cam1", "ready": true, "readers": 1, "source": "rtspSource"}]}
```

---

## 4. Enrollment endpoints (pre-existing, unchanged)

- `POST /v1/enroll` — first-boot enrollment (token-validated; optional
  service HMAC `X-Kallon-Enroll-Signature`).
- `POST /v1/enroll/confirm` — activate after WireGuard handshake.
- `GET /healthz` — liveness.

See `infra/enrollment-api/app/main.py` docstring and
`docs/project-official-reference.md` §7.

---

## 5. Non-HTTP surfaces (documented, not proxied)

- **RTSP live video:** `rtsp://<tower-vpn-ip>:8554/cam<n>`, TCP transport,
  requires WireGuard peer membership. See `docs/alert-webhook.md` §1.
- **Alert webhook:** HMAC-signed POST from tower to hub
  (`X-Kallon-Signature: sha256=<hex>` over compact sorted-key JSON). Contract
  and verification: `docs/alert-webhook.md` §2. The `sentinel-sdk` package
  ships `verify_alert()` for consumers.

---

## 6. Tower gateway (internal contract)

The control plane proxies to `http://<tower-vpn-ip>:8766`:

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/snapshot/cam{n}` | JPEG via ffmpeg |
| POST | `/api/ptz/move` | REST shape, same body as platform |
| POST | `/api/ptz/stop` | |
| GET | `/api/ptz/status?camera=n` | |
| GET | `/api/status` | Watchdog proxy |
| GET | `/api/streams` | mediamtx proxy |
| GET | `/healthz` | |

Gateway binding: `DASH_BIND=wg0` resolves the WireGuard interface address at
startup (loopback listener retained for the on-Jetson SPA). This surface is
protected only by VPN membership — see the firewall follow-up in
`planning/sdk-implementation-plan.md` §5.5.

*Contract owner: Terra platform. Changes here are breaking — version this doc.*
