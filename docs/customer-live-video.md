# Customer live video — hub HLS remux + Platform API front

**Branch:** `customer-live-video`  
**Date:** 2026-07-15  
**Goal:** Buyers watch live tower cameras in the HTTPS dashboard without joining
WireGuard, without per-hub TLS/DNS, and without Artemis joining customer meshes.

---

## Why this design

| Constraint | How this solution respects it |
|---|---|
| Artemis is temporary (Windows + ngrok today) | All video code lives in `enrollment-api` + hub agents; moving the control plane is a host/DNS change only |
| Autonomy / hub bring-up | Lightsail opens TCP **8768**; `kallon-gateway-init` installs MediaMTX + HLS agent; no human DNS |
| Buyers on Vercel (HTTPS) | Browser only talks to Artemis HTTPS; mixed-content safe |
| Artemis must not join each WG mesh | Same pattern as tower-proxy `:8767` — dial hub public IP with shared token |
| Snapshot polling overload | Live path is HLS remux, not JPEG poll |

---

## Architecture

```text
Browser (sentinel-dashboard / hls.js)
        │  HTTPS  GET /v1/towers/{id}/live/cam1/index.m3u8
        │         (+ segments under .../live/cam1/…)
        │         Auth: X-Kallon-Api-Key  or  ?api_key=
        ▼
Control plane  (enrollment-api / Platform API — “Artemis” today)
        │  HTTP   GET http://{hub}:8768/hls/{id}/cam1/index.m3u8
        │         Headers: X-Kallon-Hub-Proxy-Token
        │                  X-Kallon-Tower-Vpn-Ip
        ▼
Hub Lightsail
   ├─ kallon-hls-proxy.service   (:8768 public, token-auth)
   ├─ kallon-hub-mediamtx        (API :9997 / HLS :8888 loopback)
   │       sourceOnDemand ← rtsp://{tower-vpn}:8554/camN
   └─ wg0 ──────────────────────────────────────────────► Tower
                                                              └─ mediamtx RTSP :8554
```

NOC/raw RTSP over VPN is unchanged (`rtsp://{vpn_ip}:8554/camN`). This path is
for **buyer browsers** only.

---

## What was implemented

### Hub

| Piece | Path / unit |
|---|---|
| HLS agent | [`infra/hub/hls_proxy.py`](../infra/hub/hls_proxy.py) → `kallon-hls-proxy.service` |
| MediaMTX config | [`infra/hub/mediamtx-hub.yml`](../infra/hub/mediamtx-hub.yml) (empty paths; API adds on demand) |
| Install MediaMTX | [`scripts/kallon-hub-install-mediamtx.sh`](../scripts/kallon-hub-install-mediamtx.sh) |
| Fresh hub | [`scripts/kallon-gateway-init.sh`](../scripts/kallon-gateway-init.sh) (UFW + units) |
| Existing hub | [`scripts/kallon-gateway-ensure-hls.sh`](../scripts/kallon-gateway-ensure-hls.sh) |

On first playlist request the agent:

1. Validates the hub token + tower VPN IP header  
2. `POST/PATCH` MediaMTX path `{device_id}_camN` with  
   `source: rtsp://{vpn_ip}:8554/camN`, `sourceOnDemand: true`, idle close ~30s  
3. Proxies `http://127.0.0.1:8888/{device_id}_camN/...` back to Artemis  

### Control plane (Platform API)

| Endpoint | Role |
|---|---|
| `GET /v1/towers/{id}/live` | Catalog of `{camera, hls_url, ready}` |
| `GET /v1/towers/{id}/live/cam{n}/index.m3u8` | Playlist |
| `GET /v1/towers/{id}/live/cam{n}/{asset}` | Segments / parts / init |

Env (same `enrollment-api.env` as tower-proxy):

```text
KALLON_PROXY_VIA_HUB=1
KALLON_HUB_PROXY_TOKEN=<fleet secret>   # also used by HLS agent
KALLON_HUB_PROXY_PORT=8767
KALLON_HUB_HLS_PORT=8768                # new
KALLON_LIVE_READ_TIMEOUT=60             # optional
```

Auth for media: header **or** `?api_key=` (hls.js / Safari-friendly).

### Provisioner / Lightsail

- [`lightsail.py`](../infra/hub-provisioner/lightsail.py) opens **22, 51820/udp, 8767, 8768**  
- [`sync_lightsail_ports.py`](../infra/hub-provisioner/sync_lightsail_ports.py) syncs the same set on existing instances  
- [`interface.py`](../infra/hub-provisioner/interface.py) SCPs `hls_proxy.py`, `mediamtx-hub.yml`, install script  

### Tests

[`tests/test_platform_api.py`](../tests/test_platform_api.py) mocks hub HLS on loopback and asserts catalog + playlist + segment proxying.

---

## Cutover for existing lab hubs

On **Artemis** (PowerShell), after pulling `customer-live-video`:

```powershell
$token = "<same as KALLON_HUB_PROXY_TOKEN>"
$pem = "C:\kallon\secrets\terra-hub-ops.pem"
$repo = "C:\Users\Artemis\Documents\kallon-sentry"   # adjust
$hub = "18.220.75.237"   # from registry get-customer

# 1. Lightsail firewall (replaces full port set)
cd $repo
python infra/hub-provisioner/sync_lightsail_ports.py kallon-gateway-lab --region us-east-2
# (or the canonical name kallon-hub-cust_<id> for hubs created by the provisioner)

# 2. Install MediaMTX + HLS agent on hub
scp -i $pem `
  scripts/kallon-hub-install-mediamtx.sh `
  scripts/kallon-gateway-ensure-hls.sh `
  infra/hub/hls_proxy.py `
  infra/hub/mediamtx-hub.yml `
  ubuntu@${hub}:/tmp/

ssh -i $pem ubuntu@$hub @"
  sudo sed -i 's/\r$//' /tmp/*.sh /tmp/hls_proxy.py /tmp/mediamtx-hub.yml
  sudo mkdir -p /tmp/infra/hub /opt/kallon-hub
  sudo cp /tmp/hls_proxy.py /tmp/infra/hub/
  sudo cp /tmp/mediamtx-hub.yml /tmp/infra/hub/
  sudo cp /tmp/kallon-hub-install-mediamtx.sh /tmp/
  export HUB_PROXY_TOKEN='$token'
  export HLS_PROXY_FILE=/tmp/infra/hub/hls_proxy.py
  export MEDIAMTX_YML_SRC=/tmp/infra/hub/mediamtx-hub.yml
  sudo --preserve-env=HUB_PROXY_TOKEN,HLS_PROXY_FILE,MEDIAMTX_YML_SRC \
    bash /tmp/kallon-gateway-ensure-hls.sh
"@
```

On Artemis, ensure `KALLON_HUB_HLS_PORT=8768` in `enrollment-api.env`, restart the
NSSM enrollment service, then:

```powershell
curl.exe -s -H "X-Kallon-Api-Key: $env:KALLON_PLATFORM_API_KEY" `
  "https://<artemis-host>/v1/towers/kln_lab_000001/live"
curl.exe -s -H "X-Kallon-Api-Key: $env:KALLON_PLATFORM_API_KEY" `
  "https://<artemis-host>/v1/towers/kln_lab_000001/live/cam1/index.m3u8"
```

Expect JSON catalog, then an `#EXTM3U` playlist (may take a few seconds while
MediaMTX pulls tower RTSP — `503 stream_starting` is retryable).

---

## Dashboard / SDK notes

Preferred player: **hls.js** with header auth:

```ts
hls.config.xhrSetup = (xhr) => {
  xhr.setRequestHeader("X-Kallon-Api-Key", apiKey);
};
hls.loadSource(`${apiBase}/v1/towers/${deviceId}/live/cam1/index.m3u8`);
```

Safari native HLS: use `?api_key=` on the playlist URL (and keep relative
segment URLs from MediaMTX under the same `/live/camN/` prefix).

Deprecate snapshot polling for the live grid once tiles use HLS; keep
`GET .../snapshot/camN` for thumbnails / one-shots.

---

## Moving Artemis → permanent server

No redesign required:

1. Deploy the same `enrollment-api` + env (`KALLON_HUB_*`, Postgres)  
2. Point a real hostname + TLS at it  
3. Update dashboard / SDK base URL  

Hubs and towers keep talking the same ports. Video bytes still traverse the
control plane (acceptable for early fleet). At scale, move TLS edge toward
hubs (Cloudflare Tunnel / automated DNS) while keeping Artemis as ticket
issuer — remux stays on the hub either way.

---

## Failure cheat-sheet

| Symptom | Likely cause |
|---|---|
| `hub_proxy_misconfigured` | Token missing from process env — confirm `KALLON_HUB_PROXY_TOKEN` in `C:\kallon\config\enrollment-api.env`, pull latest control plane (env bootstrap must run **before** platform import), restart NSSM. Startup log should say `hub proxy token configured`. Empty NSSM AppEnvironmentExtra keys used to block the file; that is fixed. |
| `tower_offline` on live | Lightsail missing TCP **8768**, UFW, or `kallon-hls-proxy` down |
| `stream_starting` / empty playlist | Tower RTSP not ready, WG down, or first on-demand pull still spinning up |
| 401 from hub | Token mismatch between Artemis env and `/etc/kallon/hub-proxy.env` |
| Catalog OK, tiles black | H.265 substream — cameras need H.264 for Chromium HLS |

Health checks on hub:

```bash
curl -s http://127.0.0.1:8768/healthz
systemctl status kallon-hub-mediamtx kallon-hls-proxy
```

---

## Ports summary (public hub)

| Port | Proto | Service |
|---|---|---|
| 22 | TCP | SSH |
| 51820 | UDP | WireGuard |
| 8767 | TCP | Tower HTTP proxy (status/PTZ/snapshot) |
| 8768 | TCP | HLS remux proxy (live video) |
