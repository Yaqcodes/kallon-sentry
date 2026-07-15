## Lightsail public ports (automation)

`LightsailProvider.provision()` calls `put_instance_public_ports` with:

| Port | Proto | Purpose |
|------|-------|---------|
| 22 | TCP | SSH (ops) |
| 51820 | UDP | WireGuard |
| 8767 | TCP | Hub tower-proxy (Artemis Platform API) — from `KALLON_HUB_PROXY_PORT` |
| 8768 | TCP | Hub HLS remux proxy (live video) — from `KALLON_HUB_HLS_PORT` |

UFW on the VM is opened by `kallon-gateway-init.sh`; **Lightsail firewall is
separate** and must be set via this API (or the console).

**Existing hubs** (created before 8767/8768 was automated):

```powershell
cd C:\Users\Artemis\Documents\kallon-sentry
python infra/hub-provisioner/sync_lightsail_ports.py kallon-hub-cust_lab --region us-east-2
# instance name = Lightsail name; often kallon-hub-<customer_id>
```

**Manual (`--provider manual`) hubs:** provisioner cannot touch cloud firewall —
open TCP 8767 + 8768 (+ UDP 51820 if missing) in the provider console yourself.

Live video cutover (MediaMTX + HLS agent): see [`customer-live-video.md`](customer-live-video.md).

## Production: new hubs (automated)

Ops sets **once** on Artemis in `enrollment-api.env`:

```env
KALLON_HUB_PROXY_PORT=8767
KALLON_HUB_HLS_PORT=8768
KALLON_HUB_PROXY_TOKEN=<fleet-wide-secret>
KALLON_PROXY_VIA_HUB=1
```

`fulfill-order` / hub-provisioner then:

1. SCPs `tower_proxy.py`, `hls_proxy.py`, `mediamtx-hub.yml`, install scripts
2. Runs `kallon-gateway-init.sh` with hub proxy token + HLS ports
3. Hub enables `kallon-tower-proxy`, `kallon-hub-mediamtx`, `kallon-hls-proxy`

No copy-back to Artemis — Artemis already has the source of truth. Provisioning
**fails** if `KALLON_PROXY_VIA_HUB=1` and the token is unset.

## Lab cutover — existing hub (manual)

Run on **Artemis** + **lab hub** + **tower** after merging code. Artemis must **not** need WireGuard for Platform API PTZ/status/snapshots.

## 1. Hub (lab VPS)

From a machine that can SSH with `terra-hub-ops.pem`:

```bash
# Copy sources
scp -i terra-hub-ops.pem \
  infra/hub/tower_proxy.py \
  scripts/kallon-gateway-ensure-tower-proxy.sh \
  ubuntu@<HUB-PUBLIC-IP>:/tmp/

# On hub — pick a strong token; reuse the SAME value on Artemis
ssh -i terra-hub-ops.pem ubuntu@<HUB-PUBLIC-IP>
sudo HUB_PROXY_TOKEN='REPLACE_ME' TOWER_PROXY_FILE=/tmp/tower_proxy.py \
  bash /tmp/kallon-gateway-ensure-tower-proxy.sh

curl -s http://127.0.0.1:8767/healthz
# {"status":"ok"}
```

Confirm UFW allows `8767/tcp` from Artemis (script opens it publicly + token).

## 2. Tower (Jetson)

Gateway must listen on `wg0` (not only loopback):

```bash
# After pull of installer changes, or manually for existing tower:
sudo sed -i 's/Environment=DASH_BIND=127.0.0.1/Environment=DASH_BIND=wg0/' \
  /etc/systemd/system/kallon-tower-dashboard.service
sudo systemctl daemon-reload
sudo systemctl restart kallon-tower-dashboard

# Firewall :8766 on lo + wg0 (re-run install module if available)
sudo bash scripts/install/90-firewall.sh
```

**From the hub** (not Artemis):

```bash
curl -s --max-time 3 http://10.50.0.2:8766/api/status
# JSON with available/sensors — NOT connection refused
```

If this fails, fix WG handshake / `DASH_BIND` / iptables before continuing.

## 3. Artemis (enrollment-api)

In `C:\kallon\config\enrollment-api.env` add:

```env
KALLON_PROXY_VIA_HUB=1
KALLON_HUB_PROXY_PORT=8767
KALLON_HUB_PROXY_TOKEN=REPLACE_ME
```

Pull code, restart NSSM/service for enrollment-api.

Verify (from Artemis or laptop → ngrok/control plane):

```bash
curl -s "https://<control-plane>/v1/towers/kln_lab_000001/status" \
  -H "X-Kallon-Api-Key: <key>"
# 200 JSON — not tower_offline ConnectTimeout
```

## 4. Dashboard

Redeploy [olowu289/sentinel-dashboard](https://github.com/olowu289/sentinel-dashboard)
(`TOWER OFFLINE` / overlay banner). Hard-refresh the Vercel app.

## 5. Regression checks

- [ ] `GET /v1/customers/cust_lab/towers` still lists towers
- [ ] New enroll still auto peer-adds **tower** on hub only
- [ ] Snapshot/PTZ work without Artemis on VPN
- [ ] Hub `wg show` still has tower peer; Artemis has **no** new WG peer
