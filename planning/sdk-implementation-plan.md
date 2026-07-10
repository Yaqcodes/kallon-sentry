# Kallon SDK — Implementation Plan & Decision Log

**Terra Industries · Internal Engineering · v1.0 · July 2026**

| Related doc | Role |
|-------------|------|
| `../docs/platform-api.md` | Platform API contract (Phase 1 deliverable) |
| `../docs/project-official-reference.md` | Canonical technical reference |
| `mass-deployment-roadmap.md` | Mass deployment phases 0–8 |
| `work-plan.md` | Living task board |
| SDK repo: <https://github.com/Yaqcodes/sentinel-sdk> | Client package + developer docs site |
| `artifacts/sdk-implementation-plan.canvas.tsx` | Archived interactive plan canvas |

---

## 1. Objective

Deliver an SDK and developer reference for the Kallon platform, per the four
boss-mandated deliverables:

1. **API documentation** for snapshots, PTZ, sensor/health metadata, alerts,
   fleet data.
2. **Multi-language usage** — not just URLs/examples, but runnable client code
   in Python, JavaScript, and curl.
3. **Tower bring-up guide** — step-by-step commissioning with common errors,
   fixes, and explicit success criteria at each stage.
4. **Decoupling** — the SDK lives in its own repository
   (`sentinel-sdk`), communicates only over published network APIs, and never
   imports code from this repository.

Reference quality bar: Dahua DoLynk Developer portal (structured API
reference, per-language examples, guides).

---

## 2. Core Architectural Decision — Unified Control Plane API

**Decision:** the SDK calls **one base URL** — the Terra control plane API
(the existing enrollment FastAPI service, extended). Tower-specific
operations (PTZ, snapshot, sensor status, stream readiness) are **proxied**
by the control plane over WireGuard to the tower gateway on each Jetson.
Fleet data (customers, towers) is served directly from the Postgres registry.

**SDK consumers never call a tower directly.** Rationale:

- Towers are edge devices behind WireGuard with no public IP and no auth
  layer; they are not designed to be public API servers.
- The control plane already has a stable HTTPS endpoint and knows every
  tower's VPN IP and lifecycle state.
- A single surface means a single auth story, a single OpenAPI spec, and a
  single versioning scheme.

The tower gateway (`infra/tower-dashboard/gateway.py`) is promoted from
"optional lab dashboard backend" to the **internal proxy target**: it binds
to the tower's `wg0` address so the control plane can reach it over the VPN.
It remains invisible to SDK consumers.

**Exception:** RTSP live video cannot be HTTP-proxied. Consumers who need
live video must be WireGuard peers in the customer subnet (documented in the
SDK; a transcoding relay is flagged as a future platform capability).

---

## 3. Phases

### Phase 1 — Architecture & API design ✅

- Platform API contract written: `../docs/platform-api.md`
  (all endpoints, request/response schemas, error envelope, versioning).
- Error contract defined (see §5.3).
- Auth decision recorded (see §5.1).
- OpenAPI JSON is auto-exported by FastAPI at `/openapi.json`.

### Phase 2 — Tower gateway expansion ✅

Changes to `infra/tower-dashboard/gateway.py`:

- **New** `GET /api/snapshot/cam<n>` — single JPEG frame via `ffmpeg` from
  the local RTSP rebroadcast (`rtsp://127.0.0.1:8554/cam<n>`).
- **New** REST-shaped PTZ endpoints matching the platform proxy contract:
  `POST /api/ptz/move`, `POST /api/ptz/stop`, `GET /api/ptz/status`.
  The legacy `POST /api/ptz` relay remains for the on-Jetson SPA.
- **New** `DASH_BIND=wg0` support — the gateway resolves the `wg0` interface
  IPv4 at startup and binds to it (plus loopback via a second listener) so
  the control plane can reach it over the VPN. Default remains `127.0.0.1`
  (lab-safe); production platform mode sets `DASH_BIND=wg0` in the service
  unit.
- Structured JSON error envelope on all new endpoints:
  `{"error": {"code": "...", "message": "..."}}`.

### Phase 3 — Control plane API expansion ✅

New module `infra/enrollment-api/app/platform.py` (FastAPI router), included
by `app/main.py`:

- **Fleet endpoints** (registry-backed): list/get customers, list/get
  towers, register tower.
- **Tower proxy endpoints**: PTZ move/stop/status, snapshot, status, streams
  — forwarded over the VPN to `http://<tower-vpn-ip>:8766` with connect
  timeout 3 s / read timeout 10 s (20 s for snapshots).
- Tower offline → HTTP 503 with `{"error": {"code": "tower_offline", ...}}`.
- Tower not yet enrolled (no VPN IP) → HTTP 409 `tower_not_enrolled`.
- `httpx` added to `infra/enrollment-api/requirements.txt`.
- Tests: `tests/test_platform_api.py`.

### Phase 4 — SDK package ✅

Repo: <https://github.com/Yaqcodes/sentinel-sdk> — Python package
`sentinel_sdk`, installable via `pip install sentinel-sdk` (from source for
now; PyPI publication is an ops step).

- `SentinelClient` — typed wrapper for the entire platform API.
- `alerts.AlertVerifier` / `verify_alert()` — HMAC-SHA256 webhook
  verification.
- Typed models (`Customer`, `Tower`, `PTZStatus`, …), exception hierarchy
  (`APIError`, `AuthError`, `NotFoundError`, `TowerOfflineError`,
  `TowerNotEnrolledError`).
- Unit tests (httpx MockTransport — no live server needed).

### Phase 5 — Documentation site ✅ (scaffold)

Docusaurus site under `sentinel-sdk/docs/`:

- API reference (one page per resource: customers, towers, PTZ, snapshot,
  status/streams, enrollment, errors) with Python + JavaScript + curl
  examples on every operation.
- Guides: quick-start, alert webhook verification, RTSP consumption,
  **tower bring-up** (Terra ops audience — stage checkpoints, expected
  outputs, common-failure tables).
- Build: `npm install && npm run start` inside `docs/` (Node 18+).

---

## 4. Endpoint Map (summary — full contract in `../docs/platform-api.md`)

Control plane (SDK-facing), base path `/v1`:

| Method | Path | Type |
|--------|------|------|
| GET | `/v1/customers` | Fleet |
| GET | `/v1/customers/{customer_id}` | Fleet |
| GET | `/v1/customers/{customer_id}/towers` | Fleet |
| GET | `/v1/towers` | Fleet |
| GET | `/v1/towers/{device_id}` | Fleet |
| POST | `/v1/towers` | Fleet (factory registration) |
| POST | `/v1/towers/{device_id}/ptz/move` | Tower proxy |
| POST | `/v1/towers/{device_id}/ptz/stop` | Tower proxy |
| GET | `/v1/towers/{device_id}/ptz/status` | Tower proxy |
| GET | `/v1/towers/{device_id}/snapshot/cam{n}` | Tower proxy |
| GET | `/v1/towers/{device_id}/status` | Tower proxy |
| GET | `/v1/towers/{device_id}/streams` | Tower proxy |
| POST | `/v1/enroll`, `/v1/enroll/confirm` | Enrollment (pre-existing) |

Tower gateway (internal, `wg0` + loopback only):

| Method | Path | Status |
|--------|------|--------|
| GET | `/api/snapshot/cam{n}` | New |
| POST | `/api/ptz/move` · `/api/ptz/stop` · GET `/api/ptz/status` | New |
| POST | `/api/ptz` (legacy SPA relay) | Existing |
| GET | `/api/status` · `/api/streams` · `/api/config` · `/api/events` | Existing |

---

## 5. Decisions, Issues & Considerations (log)

### 5.1 Authentication — DEFERRED, FLAGGED ⚠

**Decision (Jul 2026): no auth on the platform API for now**, per project
direction. The fleet and proxy endpoints are currently protected only by
network position (the control plane API should NOT be exposed publicly
beyond `/v1/enroll*` until auth lands).

**Must be resolved before any external integrator uses the API.** Adding
auth later is a breaking change for SDK consumers. Options assessed:

- **API key** (static, per-integrator, `X-Kallon-Api-Key` header) — lowest
  friction, fits the existing `device.env`-style secrets model. *Recommended.*
- **JWT issued by control plane** — better multi-tenancy/scoping, more
  infrastructure.

Mitigations in place now: the SDK client already sends an optional
`api_key` (header `X-Kallon-Api-Key`) so client code will not need changes
when the server starts enforcing it. `POST /v1/towers` returns a one-time
enrollment token — treat this endpoint as Terra-ops-only until auth exists.
The reverse proxy (Caddy) should only expose `/v1/enroll*` publicly;
fleet/proxy routes should be restricted to the ops network.

### 5.2 RTSP cannot be proxied ⚠

Live video (`rtsp://<tower-vpn-ip>:8554/camN`) is not HTTP. SDK consumers
needing live video must be WireGuard peers (NOC `x.x.x.10` pattern) or
consume a future transcoding relay (HLS/MJPEG) — flagged as a Phase 8
platform capability. Documented in the SDK RTSP guide. The snapshot API is
the HTTP-friendly alternative for still frames.

### 5.3 Tower-offline error contract

Proxied requests that cannot reach the tower return **HTTP 503**:

```json
{"error": {"code": "tower_offline", "message": "...", "device_id": "kln_acme_000042"}}
```

A tower registered but not yet enrolled (no VPN IP) returns **HTTP 409**
`tower_not_enrolled`. All platform-API errors use the envelope
`{"error": {"code", "message", ...}}`. The pre-existing enrollment endpoints
keep their FastAPI `{"detail": ...}` shape (contract already shipped to
factory images — do not break it).

### 5.4 Proxy latency

Control-plane proxying adds one VPN round trip (~50–100 ms typical) to PTZ
and snapshot calls, on top of Dahua ONVIF command latency (~1.6 s p95 per
the PTZ benchmark ceiling). Acceptable for v1; documented in the SDK.

### 5.5 Gateway binding / firewall ⚠

Moving the gateway from loopback to `wg0` widens its exposure to the
customer VPN subnet. The gateway has no auth (see 5.1) — the WireGuard
subnet boundary is the control. **Installer follow-up:** module 90 iptables
should restrict `:8766` to `wg0` + `lo` (same pattern as RTSP `:8554`).
Until then, only enable `DASH_BIND=wg0` on towers whose customer subnet is
Terra-controlled.

### 5.6 Backwards compatibility of gateway responses

The on-Jetson SPA (`web/app.js`) consumes `/api/streams`, `/api/status`,
`/api/config`, `/api/events`, `/api/ptz` — their response shapes are
unchanged. New REST endpoints are additive.

### 5.7 JavaScript SDK — deferred

Ship Python + docs first; generate a TypeScript client from `/openapi.json`
(hey-api / openapi-generator) after the Python SDK has been exercised
against a live tower. JS examples in the docs are plain `fetch` for now.

### 5.8 Naming

The SDK repo/package is **sentinel-sdk** (`sentinel_sdk`, class
`SentinelClient`) per the provided repository. Platform-side naming remains
Kallon. The alert signature header remains `X-Kallon-Signature`.

### 5.9 Schema fields not yet runtime-updated

`acceptance_status` and `shipped_at` exist in the registry schema but are
not updated by runtime scripts (pre-existing gap, see official reference
§16). Fleet API returns them as-is; consumers should treat them as
best-effort until the factory flow writes them.

### 5.10 PyPI publication

`pyproject.toml` is PyPI-ready but publication (account, token, CI) is an
ops step outside this implementation. Until then integrators install from
git: `pip install git+https://github.com/Yaqcodes/sentinel-sdk`.

---

## 6. Definition of Done

- [x] Platform API contract doc (`docs/platform-api.md`)
- [x] Gateway: snapshot endpoint, REST PTZ, `wg0` binding, error envelope
- [x] Control plane: fleet + proxy endpoints, structured errors, tests pass
- [x] `sentinel-sdk`: client, models, alerts verifier, exceptions, tests pass
- [x] Docs site: API reference + quick-start + alerts + RTSP + bring-up guide
- [ ] Auth layer implemented (blocked on decision — see 5.1)
- [ ] Installer module 90 restricts `:8766` to `wg0`/`lo` (see 5.5)
- [ ] Live verification against a real tower over VPN (hardware-gated)
- [ ] PyPI publication + TypeScript client generation (post-validation)

*Terra Industries · Kallon Sentry Tower · SDK Implementation Plan v1.0 · July 2026*
