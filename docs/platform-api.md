# Kallon Platform API — Contract (v1)

**Terra Industries · Internal Engineering · July 2026**

The unified, SDK-facing HTTP API for the Kallon platform. It is served by the
control plane service (`infra/enrollment-api/`, FastAPI) and consists of:

- **Fleet endpoints** — customers/towers, backed directly by the Postgres registry.
- **Tower proxy endpoints** — PTZ, snapshots, sensor status, stream readiness;
  forwarded by the control plane over WireGuard to the tower gateway
  (`infra/tower-dashboard/gateway.py`, port `8766` on the tower's VPN IP).
- **Alert endpoints** — hub-forwarded tower events for customer dashboards
  (ingest, history, SSE fan-out).
- **Enrollment endpoints** — pre-existing first-boot flow (unchanged).

SDK consumers use **one base URL** (the control plane). They never call a
tower directly. The client library for this API is
[`sentinel-sdk`](https://github.com/Yaqcodes/sentinel-sdk). The buyer-facing
web dashboard is [`sentinel-dashboard`](https://github.com/olowu289/sentinel-dashboard).

Machine-readable spec: `GET /openapi.json` on a running control plane.

> **Auth status (July 2026):** no authentication is enforced yet — see
> `planning/sdk-implementation-plan.md` §5.1. Clients SHOULD already send
> `X-Kallon-Api-Key` so they need no changes when enforcement lands. Until
> then, do not expose fleet/proxy routes beyond the ops network; only
> `/v1/enroll*` may be public.

### Browser dashboards (Vercel + CORS)

When the buyer dashboard is hosted on a **different origin** than the control
plane (e.g. Vercel → Artemis/ngrok), the browser sends an `OPTIONS` preflight
before `GET`/`POST` with `X-Kallon-Api-Key`. Set on the API host:

```env
KALLON_CORS_ORIGINS=https://your-app.vercel.app,http://localhost:5174
```

Restart enrollment-api after changing. Use `*` only in lab. Without this,
preflight returns **405** and fleet calls fail from the browser.

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
  {
    "customer_id": "cust_acme",
    "display_name": "Acme Security Ltd",
    "vpn_subnet": "10.51.0.0/24",
    "gateway_id": "gw_acme_01",
    "gateway_endpoint": "203.0.113.42:51820",
    "gateway_public_key": "bN8xK2mP9qR4sT7vW0yZ3aB6cD1eF4gH7iJ0kL3mN6oP9q=",
    "hub_alert_url": "http://10.51.0.1:8080/alerts",
    "hub_provider": "lightsail",
    "hub_host_id": "ls_acme_hub_01",
    "status": "active",
    "created_at": "2026-06-01T10:00:00Z"
  }
]}
```

`gateway_public_key`, `gateway_id`, `hub_host_id` are included; WireGuard
private keys and HMAC keys are never returned by any endpoint.

### GET /v1/customers/{customer_id}

Single customer object (same shape as one element above). `404 not_found` if unknown:

```json
{"error": {"code": "not_found", "message": "unknown customer 'cust_nope'"}}
```

### GET /v1/customers/{customer_id}/towers

```json
{"towers": [
  {
    "device_id": "kln_acme_000001",
    "customer_id": "cust_acme",
    "group_id": "site_north_ridge",
    "vpn_ip": "10.51.0.2",
    "wg_public_key": "cO9yL3nQ0rS5tU8wX1zA4bC7dE0fG3hI6jK9lM2nO5pQ8r=",
    "status": "active",
    "acceptance_status": "passed",
    "manufactured_at": "2026-06-10T09:00:00Z",
    "enrolled_at": "2026-06-20T07:12:44Z",
    "shipped_at": "2026-06-22T14:30:00Z",
    "rtsp_base": "rtsp://10.51.0.2:8554"
  },
  {
    "device_id": "kln_acme_000002",
    "customer_id": "cust_acme",
    "group_id": null,
    "vpn_ip": null,
    "wg_public_key": null,
    "status": "registered",
    "acceptance_status": "pending",
    "manufactured_at": "2026-06-11T11:00:00Z",
    "enrolled_at": null,
    "shipped_at": null,
    "rtsp_base": null
  }
]}
```

`rtsp_base` is derived (`rtsp://{vpn_ip}:8554`); null until enrolled. Camera
paths are `cam1`…`camN` (count from factory `CAMERA_IPS`). `claim_code` and
`enrollment_token_hash` are **not** returned.

### GET /v1/towers

All towers across customers (Terra ops). Optional `?status=active` filter:

```json
{"towers": [
  {"device_id": "kln_acme_000001", "customer_id": "cust_acme", "vpn_ip": "10.51.0.2", "status": "active", "rtsp_base": "rtsp://10.51.0.2:8554"}
]}
```

### GET /v1/towers/{device_id}

Single tower object (same shape as one element in the list above).

### POST /v1/towers

Factory registration (**Terra-ops-only** until auth lands — returns a
one-time secret).

Request:

```json
{"customer_id": "cust_acme", "serial": 43, "group_id": "site_harbor"}
```

Response `201`:

```json
{
  "device_id": "kln_acme_000043",
  "customer_id": "cust_acme",
  "claim_code": "clm_8f3kLmNpQrStUvWxYz01ab",
  "enrollment_token": "enr_a7Kx9mP2qR5sT8vW1yZ4bC7dE0fG3hI6jK9lM2nO5pQ8rS1tU4vW7xY0zA3b"
}
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

Absolute move request:

```json
{"camera": 1, "mode": "absolute", "pan": 0.5, "tilt": -0.2, "zoom": 0.0}
```

Continuous jog request (velocities in `[-1, 1]`, `seconds` ≤ 10):

```json
{"camera": 2, "mode": "continuous", "pan": 0.3, "tilt": 0.0, "zoom": 0.0, "seconds": 0.5}
```

`camera` defaults to 1. Success response:

```json
{"ok": true, "result": {"ok": true, "round_trip_ms": 1240.5}}
```

Tower gateway error (proxied as `502 tower_error`):

```json
{"error": {"code": "tower_error", "message": "PTZ daemon error", "device_id": "kln_acme_000001"}}
```

### POST /v1/towers/{device_id}/ptz/stop

Stop motion:

```json
{"camera": 1}
```

Return to home position:

```json
{"camera": 1, "home": true}
```

Response:

```json
{"ok": true, "result": {}}
```

### GET /v1/towers/{device_id}/ptz/status?camera=1

```json
{
  "ok": true,
  "result": {
    "pan": 0.12,
    "tilt": -0.05,
    "zoom": 0.35,
    "pan_deg": 43.2,
    "tilt_deg": -1.8,
    "zoom_ratio": 2.4
  }
}
```

ONVIF-normalized `pan`/`tilt`/`zoom` are always present; degree/ratio fields
are included when the tower PTZ daemon provides them.

### GET /v1/towers/{device_id}/snapshot/cam{n}

Returns `200` with `Content-Type: image/jpeg` — raw JPEG bytes (not JSON).
Captured on the tower from `rtsp://127.0.0.1:8554/cam<n>`; typically 80–200 KB.

`404 not_found` if camera index is out of range:

```json
{"error": {"code": "not_found", "message": "camera 9 out of range", "device_id": "kln_acme_000001"}}
```

### GET /v1/towers/{device_id}/status

Watchdog sensor/health snapshot, proxied from the tower's status API:

```json
{
  "available": true,
  "device_id": "kln_acme_000001",
  "poll_interval_sec": 10,
  "mpu_present": true,
  "uptime_sec": 86412.4,
  "timestamp_utc": "2026-07-13T10:22:41Z",
  "door": {"open": false},
  "light": {"exposed": false},
  "impact": {
    "threshold_mg": 150,
    "last_delta_mg": 12.4,
    "last_impact_utc": null
  },
  "temperature": {
    "celsius": 46.2,
    "zone": "thermal_zone0",
    "critical": false,
    "trigger_c": 80.0,
    "clear_c": 75.0
  },
  "disk": {
    "enabled": true,
    "faulted": false,
    "space_free_gb": 1737.4,
    "space_total_gb": 1832.7,
    "space_used_gb": 95.3,
    "percentage_used": 5.2,
    "available_spare": 100,
    "smart_temp_c": 50
  },
  "streams": [
    {"path": "cam1", "ok": true},
    {"path": "cam2", "ok": true},
    {"path": "cam3", "ok": false},
    {"path": "cam4", "ok": true}
  ]
}
```

Tower reachable but watchdog status API down (`200`, not an error envelope):

```json
{"available": false, "error": "connection refused"}
```

### GET /v1/towers/{device_id}/streams

mediamtx path readiness:

```json
{
  "available": true,
  "paths": [
    {"name": "cam1", "ready": true, "readers": 1, "source": "rtspSource"},
    {"name": "cam2", "ready": true, "readers": 0, "source": "rtspSource"},
    {"name": "cam3", "ready": false, "readers": 0, "source": null},
    {"name": "cam4", "ready": true, "readers": 2, "source": "rtspSource"}
  ]
}
```

mediamtx unreachable:

```json
{"available": false, "error": "URLError: timed out", "paths": []}
```

---

## 4. Alert endpoints (dashboard)

Hub listeners verify tower HMAC signatures and forward accepted alerts to the
control plane. Set on each customer hub:

```text
ALERT_FORWARD_URL=https://<control-plane>/v1/alerts/ingest
```

Optional ingest gate (recommended on public endpoints):

```text
KALLON_ALERT_INGEST_TOKEN=<secret>
X-Kallon-Ingest-Token: <secret>
```

### 4.1 Ingest (hub → control plane)

`POST /v1/alerts/ingest`

Request body (verbatim tower alert — compact JSON with sorted keys for HMAC on
the tower→hub leg; ingest accepts the forwarded body as-is):

```json
{
  "device_id": "kln_acme_000001",
  "timestamp_utc": "2026-07-13T10:31:12Z",
  "nonce": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "alert_type": "tamper_door_open",
  "severity": "critical",
  "details": {
    "gpio_pin": 31,
    "level": "HIGH",
    "boot_state": true
  }
}
```

Response `201` (first time this idempotency key is seen):

```json
{
  "status": "accepted",
  "alert": {
    "device_id": "kln_acme_000001",
    "customer_id": "cust_acme",
    "alert_type": "tamper_door_open",
    "kind": "tamper_door_open",
    "severity": "critical",
    "timestamp_utc": "2026-07-13T10:31:12Z",
    "nonce": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "details": {"gpio_pin": 31, "level": "HIGH", "boot_state": true},
    "received_utc": "2026-07-13T10:31:12Z"
  }
}
```

Duplicate (`200`):

```json
{"status": "duplicate", "alert": {"device_id": "kln_acme_000001", "alert_type": "tamper_door_open", "...": "…"}}
```

### 4.2 History

`GET /v1/alerts?customer_id=cust_acme&device_id=kln_acme_000001&limit=50`

`GET /v1/customers/cust_acme/alerts?limit=50`

```json
{
  "alerts": [
    {
      "device_id": "kln_acme_000001",
      "customer_id": "cust_acme",
      "alert_type": "wind_high",
      "kind": "wind_high",
      "severity": "warning",
      "timestamp_utc": "2026-07-13T10:28:04Z",
      "nonce": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "details": {"kmh": 34, "gust": 41, "trip": 30},
      "received_utc": "2026-07-13T10:28:05Z"
    },
    {
      "device_id": "kln_acme_000001",
      "customer_id": "cust_acme",
      "alert_type": "tamper_door_open",
      "kind": "tamper_door_open",
      "severity": "critical",
      "timestamp_utc": "2026-07-13T10:31:12Z",
      "nonce": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
      "details": {"gpio_pin": 31, "level": "HIGH", "boot_state": true},
      "received_utc": "2026-07-13T10:31:12Z"
    }
  ]
}
```

Newest first. Requires `X-Kallon-Api-Key` when `KALLON_PLATFORM_API_KEY` is set.

### 4.3 Live stream (SSE)

`GET /v1/events?customer_id=cust_acme`

`Content-Type: text/event-stream`. On connect, replays recent history, then
pushes new alerts. Each event:

```text
data: {"device_id":"kln_acme_000001","customer_id":"cust_acme","alert_type":"stream_fail","kind":"stream_fail","severity":"warning","timestamp_utc":"2026-07-13T10:35:00Z","nonce":"9b8c7d6e-5f4a-3210-9876-543210fedcba","details":{"url":"rtsp://127.0.0.1:8554/cam3","exit_code":1},"received_utc":"2026-07-13T10:35:01Z"}

```

---

## 5. Enrollment endpoints (pre-existing, unchanged)

- `POST /v1/enroll` — first-boot enrollment (token-validated; optional
  service HMAC `X-Kallon-Enroll-Signature`).
- `POST /v1/enroll/confirm` — activate after WireGuard handshake.
- `GET /healthz` — liveness.

See `infra/enrollment-api/app/main.py` docstring and
`docs/project-official-reference.md` §7.

---

## 6. Non-HTTP surfaces (documented, not proxied)

- **RTSP live video:** `rtsp://<tower-vpn-ip>:8554/cam<n>`, TCP transport,
  requires WireGuard peer membership. See `docs/alert-webhook.md` §1.
- **Alert webhook:** HMAC-signed POST from tower to hub
  (`X-Kallon-Signature: sha256=<hex>` over compact sorted-key JSON). Contract
  and verification: `docs/alert-webhook.md` §2. The `sentinel-sdk` package
  ships `verify_alert()` for consumers.

---

## 7. Tower gateway (internal contract)

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
