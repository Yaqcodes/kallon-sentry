# Sentinel Console (on-Jetson dashboard UI)

The React front-end for the **on-Jetson** Sentinel tower dashboard. It is an
internal, loopback-only console: it talks **directly to the tower's local
gateway** (`gateway.py`) over `127.0.0.1`, never to the hub/cloud.

- Quad camera view with MJPEG (preferred) / HLS (fallback) live video
- PTZ control (pan/tilt/zoom/home) relayed to the on-device PTZ daemon
- Live sensor grid + SSE alert feed from the watchdog status API
- Single tower only (this Jetson) — no fleet/hub concepts

## Develop

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api + SSE to a gateway on :8766
```

Point it at a real tower by SSH tunnelling the gateway to your machine:

```bash
ssh -N -L 8766:127.0.0.1:8766 <user>@<tower>
```

## Build (what ships to the Jetson)

```bash
npm run build      # type-checks, then writes the static bundle into ../web
```

`../web` is committed and is exactly what the installer (`85-tower-dashboard.sh`)
syncs to `/opt/kallon/tower-dashboard/web` and `gateway.py` serves. **The Jetson
never runs Node or npm** — it only serves these static files. After changing the
UI, run `npm run build` and commit the regenerated `../web`.

## Gateway API consumed

| Endpoint | Use |
|---|---|
| `GET /api/config` | device id + camera list (paths, HLS/MJPEG URLs) |
| `GET /api/streams` | mediamtx per-path readiness → camera ONLINE/OFFLINE |
| `GET /api/status` | watchdog snapshot → sensor grid |
| `GET /api/events` | SSE alert feed |
| `POST /api/ptz` | relay a PTZ command to the daemon |
