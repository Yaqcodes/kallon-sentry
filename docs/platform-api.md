# Kallon Platform API — Contract (v1)

**Terra Industries · Internal Engineering · July 2026**

The unified, SDK-facing HTTP API for the Kallon platform. It is served by the
control plane service (`infra/enrollment-api/`, FastAPI) and consists of:

- **Fleet endpoints** — customers/towers, backed directly by the Postgres registry.
- **Tower proxy endpoints** — PTZ, snapshots, sensor status, stream readiness,
  continuous NVR toggle; the control plane calls an authenticated **hub
  tower-proxy agent** on the customer hub's public IP (`:8767`). The hub
  forwards over WireGuard to the tower gateway (`infra/tower-dashboard/gateway.py`,
  `:8766` on the tower VPN IP). Artemis does **not** join customer WireGuard meshes.
- **Live video endpoints** — HLS playlists/segments from the hub HLS agent
  (`:8768`) which remuxes tower RTSP via local MediaMTX. See
  [`docs/customer-live-video.md`](customer-live-video.md).
- **Cloud recordings** — S3/B2 segment registry (ingest, list, playback,
  download, delete, retention, ops purge). See §3c.
- **Alert endpoints** — hub-forwarded tower events for customer dashboards
  (ingest, history, SSE fan-out).
- **Enrollment endpoints** — pre-existing first-boot flow (unchanged).

SDK consumers use **one base URL** (the control plane). They never call a
tower directly. The client library for this API is
[`sentinel-sdk`](https://github.com/Yaqcodes/sentinel-sdk). The buyer-facing
web dashboard is [`sentinel-dashboard`](https://github.com/olowu289/sentinel-dashboard).

Machine-readable / Swagger:

- `GET /openapi.json` — OpenAPI 3 schema
- `GET /docs` — Swagger UI
- `GET /redoc` — ReDoc

> **Auth status (July 2026):** if `KALLON_PLATFORM_API_KEY` is set, clients must
> send `X-Kallon-Api-Key` (or `?api_key=` for HLS media). Soft gate when unset.

### Browser dashboards (Vercel + CORS)

When the buyer dashboard is hosted on a **different origin** than the control
plane (e.g. Vercel → Artemis/ngrok), the browser sends an `OPTIONS` preflight
before `GET`/`POST` with `X-Kallon-Api-Key`. Set on the API host:

```env
KALLON_CORS_ORIGINS=https://your-app.vercel.app,http://localhost:5174
```

Restart enrollment-api after changing. Use `*` only in lab. Without this,
preflight returns **405** and fleet calls fail from the browser.

**ngrok free tier:** browser `GET` requests may still fail CORS even when
`OPTIONS` returns 200 — ngrok injects an HTML interstitial without
`Access-Control-Allow-Origin`. The TypeScript SDK sends
`ngrok-skip-browser-warning: 1` when the base URL contains `ngrok`. SSE
(`/v1/events` via `EventSource`) cannot send that header; use a same-origin
proxy or a non-ngrok API URL for live alerts.

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
| 401 | `unauthorized` | Bad/missing `X-Kallon-Api-Key` or ingest token |
| 404 | `not_found` | Unknown customer/tower/camera/segment |
| 409 | `tower_not_enrolled` | Tower registered but has no VPN IP yet |
| 409 | `conflict` | e.g. `POST /v1/towers` device already exists |
| 422 | `invalid_request` | Malformed body/params |
| 502 | `tower_error` | Tower reached, but its gateway returned an error |
| 502 | `hub_proxy_auth_failed` | Hub rejected `X-Kallon-Hub-Proxy-Token` (Artemis↔hub mismatch) |
| 502 | `s3_error` | S3/B2 presign or delete failure |
| 503 | `hub_not_provisioned` | Customer has no `gateway_endpoint` in registry (not a live outage) |
| 503 | `hub_proxy_misconfigured` | Control plane missing `KALLON_HUB_PROXY_TOKEN` |
| 503 | `hub_proxy_unreachable` | Control plane cannot connect to hub `:8767` (tower may still be online) |
| 503 | `hub_proxy_timeout` | Hub `:8767` connected but timed out (hub hung or hub→tower slow) |
| 503 | `hub_hls_unreachable` | Control plane cannot reach hub HLS `:8768` |
| 503 | `hub_mediamtx_unreachable` | Hub HLS agent cannot reach local MediaMTX |
| 503 | `tower_offline` | Hub → tower `:8766` failed (VPN / gateway / reboot) |
| 503 | `tower_unreachable` / `tower_timeout` | Direct (lab) control-plane → tower gateway failure |
| 503 | `registry_unavailable` | Registry DB unreachable |
| 503 | `s3_not_configured` | Playback/download without Platform S3 credentials |
| 503 | `stream_starting` | Live HLS not ready yet (clients should retry) |

Messages name the failing hop (`control-plane→hub`, `hub→tower`, etc.) so
"tower offline" is reserved for actual tower-gateway failures — not hub outages.

`tower_offline` example (hub reached, tower gateway did not):

```json
{"error": {
  "code": "tower_offline",
  "message": "hub → tower 10.50.0.2:8766/api/status failed (URLError: timed out) — wrong VPN IP, WireGuard down, gateway not listening on :8766, or tower rebooting",
  "device_id": "kln_acme_000042",
  "hop": "hub→tower:8766",
  "vpn_ip": "10.50.0.2"
}}
```

`hub_proxy_unreachable` example (tower may still be fine):

```json
{"error": {
  "code": "hub_proxy_unreachable",
  "message": "control-plane could not connect to hub tower-proxy at 18.x.x.x:8767 (ConnectError) — tower-proxy down, DNS/firewall, or port 8767 closed (tower may still be online on WireGuard)",
  "device_id": "kln_acme_000042",
  "hop": "control-plane→hub:8767",
  "cause": "ConnectError"
}}
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

### GET / PUT /v1/towers/{device_id}/recording

Continuous NVR toggle (MediaMTX + `RECORD_ENABLE` persist on tower). Proxied to
tower `GET|PUT /api/recording`.

**PUT body:** `{"enabled": true|false}`

### GET /v1/towers/{device_id}/recording (response excerpt)

```json
{
  "enabled": true,
  "desired": true,
  "effective": true,
  "record_path": "/var/kallon/recordings",
  "delete_after": "168h",
  "delete_after_effective": "168h",
  "segment_duration": "15m",
  "upload_enable": true,
  "paths": [
    {"name": "cam1", "record": true, "ready": true}
  ],
  "disk": {
    "mount": "/var/kallon/recordings",
    "space_total_gb": 1832.7,
    "space_free_gb": 1700.0,
    "space_used_gb": 132.7,
    "source": "/dev/nvme0n1p1",
    "on_nvme": true
  },
  "upload": {"available": true, "pending": 2, "last_upload_at": "2026-07-15T12:30:00Z"},
  "warnings": []
}
```

Field notes (tower `device.env` / `record_settings.py`):

- `segment_duration` ← `RECORD_MEDIAMTX_SEGMENT_FILE_DURATION` (default `15m`)
- `delete_after` / `delete_after_effective` ← `RECORD_MEDIAMTX_DELETE_AFTER` (default `168h`)
- `upload.available` is false when `.upload-state.json` is missing

PUT success returns the same status object plus `ok`, `persist_ok`, and optional
`persist_error` / `path_errors`.

---

## 3b. Live video (HLS via hub remux)

Full design + cutover: [`customer-live-video.md`](customer-live-video.md).

Buyers play HLS from the **control plane** (not the hub). Artemis dials
`http://{hub}:8768/hls/...` with the same `KALLON_HUB_PROXY_TOKEN` used for
`:8767`.

### GET /v1/towers/{device_id}/live

```json
{
  "device_id": "kln_lab_000001",
  "protocol": "hls",
  "note": "Play hls_url with hls.js; pass api_key via xhrSetup or ?api_key=",
  "cameras": [
    {
      "camera": 1,
      "path": "cam1",
      "ready": true,
      "hls_url": "https://<control-plane>/v1/towers/kln_lab_000001/live/cam1/index.m3u8"
    }
  ]
}
```

### GET /v1/towers/{device_id}/live/cam{n}/index.m3u8

HLS playlist (`application/vnd.apple.mpegurl`). May return `503` with
`stream_starting` while MediaMTX first pulls tower RTSP — clients should retry.

### GET /v1/towers/{device_id}/live/cam{n}/{asset}

Segments / fMP4 parts under the same auth as the playlist.

Env: `KALLON_HUB_HLS_PORT` (default `8768`), `KALLON_LIVE_READ_TIMEOUT` (default `90`).

---

## 3c. Cloud recordings (S3 / Backblaze B2)

Tower upload workers (`scripts/kallon-recording-uploader.py`) close MediaMTX
segments (default **15m**), remux fMP4 → progressive MP4 (`+faststart`), upload
to a shared **Backblaze B2** bucket (S3-compatible), register metadata here, then
keep or delete locals per `RECORD_LOCAL_DELETE_AFTER_UPLOAD` / retention policy.

Object layout: `{device_id}/cam{N}/{filename}.mp4`

**Tenant isolation:** list/get/playback/download/delete are scoped by
`customer_id`. A buyer session must only query its own customer.

**Cloud retention:** default **30 days** (`platform_config.recording_retention_days`
or `KALLON_RECORDING_RETENTION_DAYS` on Platform). Independent of tower local
retention (`RECORD_MEDIAMTX_DELETE_AFTER`, default `168h`).

Public segment fields never include `s3_bucket` / `s3_key`.

### POST /v1/recordings/ingest (tower → platform)

Auth: `X-Kallon-Ingest-Token` when `KALLON_RECORDING_INGEST_TOKEN` is set
(falls back to `KALLON_ALERT_INGEST_TOKEN`). Soft-open when neither is set.

Request:

```json
{
  "device_id": "kln_acme_000042",
  "camera": 1,
  "filename": "2026-07-15_12-00-00-000000.mp4",
  "s3_bucket": "kallon-recordings",
  "s3_key": "kln_acme_000042/cam1/2026-07-15_12-00-00-000000.mp4",
  "size_bytes": 52428800,
  "sha256_hex": "…",
  "started_at": "2026-07-15T12:00:00Z",
  "ended_at": "2026-07-15T12:15:00Z",
  "duration_sec": 900
}
```

`sha256_hex`, `ended_at`, and `duration_sec` are optional. `camera` is 1–32.

Response `201`:

```json
{
  "segment": {
    "segment_id": "<uuid>",
    "customer_id": "cust_acme",
    "device_id": "kln_acme_000042",
    "camera": 1,
    "filename": "2026-07-15_12-00-00-000000.mp4",
    "size_bytes": 52428800,
    "sha256_hex": "…",
    "started_at": "2026-07-15T12:00:00+00:00",
    "ended_at": "2026-07-15T12:15:00+00:00",
    "uploaded_at": "2026-07-15T12:16:02+00:00",
    "duration_sec": 900
  }
}
```

Errors: `401`, `404` (unknown device), `422`, `503 registry_unavailable`.

### POST /v1/recordings/purge-device (ops / tower reset)

Removes **registry rows** for a device. **Does not delete S3/B2 objects** —
operators must purge the bucket separately if needed.

Auth: same ingest gate as `/v1/recordings/ingest`.

Request:

```json
{"device_id": "kln_acme_000042"}
```

Response `200`:

```json
{"device_id": "kln_acme_000042", "deleted_segments": 12}
```

### GET /v1/customers/{customer_id}/recordings

Auth: platform API key. Query: `device_id`, `camera`, `from_ts`, `to_ts`
(ISO-8601 bounds on `started_at`), `limit` (default 100), `offset` (default 0).

```json
{
  "customer_id": "cust_acme",
  "retention_days": 30,
  "segments": [
    {
      "segment_id": "…",
      "customer_id": "cust_acme",
      "device_id": "kln_acme_000042",
      "camera": 1,
      "filename": "2026-07-15_12-00-00-000000.mp4",
      "size_bytes": 52428800,
      "sha256_hex": "…",
      "started_at": "2026-07-15T12:00:00+00:00",
      "ended_at": "2026-07-15T12:15:00+00:00",
      "uploaded_at": "2026-07-15T12:16:02+00:00",
      "duration_sec": 900
    }
  ]
}
```

### GET /v1/customers/{customer_id}/recordings/{segment_id}

Returns one segment if it belongs to that customer (cross-tenant IDs → `404`).

```json
{"segment": { "segment_id": "…", "customer_id": "cust_acme", "…": "…" }}
```

### GET /v1/customers/{customer_id}/recordings/{segment_id}/playback

Auth: platform API key. Requires S3 configured (`KALLON_S3_BUCKET`,
`KALLON_S3_ENDPOINT`, `KALLON_S3_REGION`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`). Optional TTL: `KALLON_S3_PRESIGN_TTL_SEC`
(default 3600, clamped 60–86400).

```json
{"segment_id": "…", "url": "https://…", "expires_in": 3600}
```

Errors: `503 s3_not_configured`, `502 s3_error`, `404`.

### GET /v1/customers/{customer_id}/recordings/{segment_id}/download

Same JSON shape as playback, but the presigned URL forces
`Content-Disposition: attachment; filename="<segment.filename>"`.

### DELETE /v1/customers/{customer_id}/recordings/{segment_id}

Deletes the S3 object when S3 is configured, then the registry row. If S3 is
not configured, deletes the registry row only. If S3 delete fails, the
registry row is kept (`502 s3_error`).

```json
{"status": "deleted", "segment_id": "…"}
```

### GET /v1/platform/recording-retention

Auth: platform API key.

```json
{"retention_days": 30}
```

Resolution: `KALLON_RECORDING_RETENTION_DAYS` env →
`platform_config.recording_retention_days` → default `30` (min 1).

### PUT /v1/platform/recording-retention

```json
{"retention_days": 45}
```

Persists to `platform_config`. Response: `{"retention_days": 45}`.
`422` if not a positive integer.

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

### Control plane → hub → tower

Artemis does **not** dial tower VPN IPs. Default path (`KALLON_PROXY_VIA_HUB=1`):

```text
SDK → Artemis /v1/towers/{id}/…
         → http://{hub-public-host}:8767/proxy/{id}/api/…
              headers: X-Kallon-Hub-Proxy-Token, X-Kallon-Tower-Vpn-Ip
         → hub agent → http://{tower-vpn-ip}:8766/api/…
```

| Env (Artemis) | Purpose |
|---------------|---------|
| `KALLON_HUB_PROXY_PORT` | Hub agent port (default `8767`) |
| `KALLON_HUB_PROXY_TOKEN` | Shared secret with hub `HUB_PROXY_TOKEN` |
| `KALLON_PROXY_VIA_HUB` | `1` (default) or `0` for lab when Artemis is a WG NOC peer |

Hub host is derived from `customers.gateway_endpoint` (host before `:51820`).
Deploy / migrate hubs with `scripts/kallon-gateway-ensure-tower-proxy.sh`.

### Tower gateway routes (on `:8766`)

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/config` | Device id + camera list (lab SPA) |
| GET | `/api/events` | SSE alerts (lab) |
| GET | `/api/snapshot/cam{n}` | JPEG via ffmpeg |
| POST | `/api/snapshot` | Save JPEG to disk (SPA) |
| POST | `/api/ptz` | SPA JSON-RPC relay to PTZ daemon |
| POST | `/api/ptz/move` | REST shape, same body as platform |
| POST | `/api/ptz/stop` | |
| GET | `/api/ptz/status?camera=n` | |
| GET | `/api/status` | Watchdog proxy |
| GET | `/api/streams` | mediamtx proxy |
| GET | `/api/recording` | NVR status + upload queue |
| PUT / POST | `/api/recording` | Enable/disable continuous recording |
| GET | `/api/recordings` | Local MP4 list (`?camera=&limit=`) — **not** Platform-proxied |
| GET | `/api/recordings/file/cam{n}/{file}.mp4` | Range stream (remux cache for playback) |
| POST | `/ingest/alerts` | Local listener → gateway |
| GET | `/healthz` | |

Buyer historical playback uses cloud routes under `/v1/customers/…/recordings…`.
Local `/api/recordings*` is Jetson/lab only.

Gateway binding: `DASH_BIND=wg0` resolves the WireGuard interface address at
startup (loopback listener retained for the on-Jetson SPA). Port `8766` is
firewalled to `lo` + `wg0` only (`scripts/install/90-firewall.sh`).

*Contract owner: Terra platform. Changes here are breaking — version this doc.*
