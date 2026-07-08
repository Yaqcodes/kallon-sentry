# Jetson Tower Setup Guide

**Terra Industries · Internal Engineering**

Step-by-step guide for setting up a new Sentry Tower (Jetson Orin Nano) from a fresh
image to enrolled and streaming. Covers one tower at a time — repeat for each unit.

| Related | Role |
|---------|------|
| `docs/identity-and-secrets.md` §3 | Permissions + secrets reference |
| `docs/field-test-setup.md` §5 | Detailed bench walkthrough |
| `docs/order-fulfillment.md` Phase 3 | Factory production flow |
| `docs/architecture-setup-guide.md` Phase 7–8 | Full layered context |

---

## Prerequisites

Before touching the Jetson, you need these ready on the control plane (Artemis):

- [ ] Control plane is up: Postgres, enrollment API at `https://enroll.<domain>/v1`
- [ ] `kallon-fulfill-order` has been run for this customer — you have:
  - `device_kln_<slug>_00000N.env` (unique per tower)
  - `alert.key` (shared across all towers on the same hub)
- [ ] Hub VPS is provisioned and `status=active` in the registry
- [ ] You know the Jetson's LAN IP and SSH user

> **Fresh JetPack flash:** If the board is bare, flash it using NVIDIA SDK Manager
> before proceeding. This repo does not include flashing instructions. After flash,
> complete initial OS setup (set a user, enable SSH) then come back here.

---

## Step 1 — Install Chromium (if needed)

If the tower will run the local kiosk dashboard, install Chromium first.

```bash
sudo apt update
sudo apt install -y chromium-browser
```

If Chromium fails to launch after install, see the note at the bottom of this doc.

---

## Step 2 — Clone the repo on the Jetson

SSH into the Jetson and clone the `field-test` branch:

```bash
cd ~
git clone https://github.com/Yaqcodes/kallon-sentry.git kallon
cd kallon
git checkout field-test
git pull origin field-test
```

Verify:

```bash
ls scripts/kallon-jetson-install.sh   # must exist
```

---

## Step 3 — Copy factory files from the control plane

Run these commands **on Artemis** (or any machine that can SSH to both):

```powershell
$JETSON   = "YOUR_USER@JETSON_LAN_IP"      # e.g. khalifa@192.168.1.246
$HUB_HOST = "YOUR_HUB_PUBLIC_IP"           # from fulfillment manifest
$PEM      = "C:\kallon\secrets\terra-hub-ops.pem"
$FACTORY  = "C:\kallon\factory\cust_<slug>"

# 1. Per-tower config (unique device_id + enrollment_token)
scp "$FACTORY\device_kln_<slug>_000001.env" "${JETSON}:/tmp/device.env"

# 2. Hub alert key — same file for every tower on this hub
#    Fetch it from the hub if you don't have it locally yet:
ssh -i $PEM "ubuntu@${HUB_HOST}" "sudo cat /etc/kallon/alert.key" |
  Set-Content -NoNewline -Encoding ascii "$FACTORY\alert.key"
scp "$FACTORY\alert.key" "${JETSON}:/tmp/alert.key"
```

> **alert.key** is per-hub, not per-tower. If you're setting up the second or third
> tower on the same hub, the same `alert.key` file is reused.

---

## Step 4 — Install config files on the Jetson

SSH to the Jetson and run:

```bash
# Set to your Jetson login username (the account you SSH'd in as)
RUNTIME_USER="$(id -un)"   # e.g. sentinel, khalifa, ubuntu — whatever your login is

# Create the config directory
sudo install -d -m 0750 -o root -g "$RUNTIME_USER" /etc/kallon

# Install device.env (per-tower)
sudo install -m 0640 -o root -g "$RUNTIME_USER" /tmp/device.env /etc/kallon/device.env
sudo sed -i 's/\r$//' /etc/kallon/device.env   # strip Windows CRLF

# Install alert.key (per-hub)
sudo install -m 0640 -o root -g "$RUNTIME_USER" /tmp/alert.key /etc/kallon/alert.key
sudo sed -i 's/\r$//' /etc/kallon/alert.key
```

Open `device.env` and fill in any fields that are not already set by fulfillment:

```bash
sudoedit /etc/kallon/device.env
```

Key fields to verify:

| Field | What to set |
|-------|-------------|
| `RUNTIME_USER` | **Your Jetson login** (e.g. `sentinel`). Set this explicitly — see note below |
| `DEVICE_ID` | `kln_<slug>_<6 digits>` — from `register-tower` output |
| `CUSTOMER_ID` | `cust_<slug>` |
| `ENROLLMENT_TOKEN` | `enr_…` — from `register-tower` output |
| `ENROLLMENT_URL` | `https://enroll.<domain>/v1` |
| `WAN_IFACE` | WAN interface name — run `ip link` if unsure |
| `CAMERA_IFACE` | Camera ethernet interface name |
| `CAMERA_IPS` | Camera IP(s), comma-separated |
| `CAMERA_PASSWORD` | Dahua admin password |

> **Set `RUNTIME_USER` explicitly for a golden image.** Setting
> `RUNTIME_USER=<your-login>` in `device.env` removes all ambiguity and documents
> intent, guaranteeing the same result on every device and every invocation
> method. If left unset, the installer falls back to `SUDO_USER` then `logname`,
> and **fails loudly** if both are empty (e.g. run from a plain root shell).

**Common interface names on Jetson Orin Nano:**

```
Wi-Fi (WAN):      wlP1p1s0
Camera ethernet:  enP8p1s0
```

Verify the file is correct:

```bash
grep -E "DEVICE_ID|ENROLLMENT_URL|CAMERA_IPS" /etc/kallon/device.env
ls -la /etc/kallon/
```

---

## Step 5 — Run the installer

```bash
cd ~/kallon
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env
```

This runs modules **00 → 99** in order. The full list:

| Module | What it does | Pass looks like |
|--------|--------------|-----------------|
| **00-preflight** | Validates env, ID formats, arm64 | `preflight passed for kln_…` |
| **10-packages** | apt install wireguard, ffmpeg, iptables | `packages installed` |
| **20-users-groups** | GPIO/I2C/video groups, sudoers | `added to gpio` or already in group |
| **30-network-policy** | Wi-Fi default route, camera eth no gateway | `ASSERT ok: <camera IP> via <eth>` |
| **40-wireguard** | Userspace WG drop-in, enable `wg-quick@wg0` | `wg-quick@wg0 up` (or WARN if no `wg0.conf` yet — OK pre-enroll) |
| **50-mediamtx** | Download mediamtx, render `/etc/mediamtx.yml` | `rendered /etc/mediamtx.yml for N camera(s)` |
| **60-camera-route** | Systemd unit pins camera IPs to camera iface | `rendered kallon-camera-route.service` |
| **70-app** | Copy app to `/opt/kallon`, pip install | `app installed to /opt/kallon` |
| **80-watchdogs** | Watchdog + PTZ systemd units, generate `alert.key` if missing | `rendered kallon-watchdog.service` |
| **85-tower-dashboard** | Local Sentinel kiosk UI (**only if `ENABLE_TOWER_DASHBOARD=1`**) | `disabled` on production towers |
| **90-firewall** | iptables: TCP 8554 → lo + wg0 only | `firewall rules applied` |
| **99-acceptance** | Runs acceptance checks | See Step 7 |

The on-Jetson dashboard is a React app (`infra/tower-dashboard/sentinel-console/`)
built to static files in `infra/tower-dashboard/web/` and served by `gateway.py` on
loopback (`http://127.0.0.1:8766`). It talks **only to the tower** (config, streams,
status, alerts, PTZ) — not to the hub. To change the UI, rebuild on a dev machine
with Node and commit the updated `web/` bundle:

```bash
cd infra/tower-dashboard/sentinel-console && npm install && npm run build
```

If a module fails, fix the specific issue and re-run just that module:

```bash
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --only-module 30
```

---

## Step 6 — Enable enrollment service

The enrollment service runs once on boot, enrolls the tower, then never runs again.
Install it now so it fires on the next boot (or at the customer site on first boot):

```bash
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --only-module 75
```

Verify it is enabled:

```bash
systemctl is-enabled kallon-enroll.service   # → enabled
systemctl is-enabled kallon-enroll.timer     # → enabled
```

> **Why a service and not a manual script?** On production towers, enrollment happens
> automatically at the customer site on first boot with no ops intervention required.
>
> **Why a timer too?** `kallon-enroll.timer` re-runs the enroll flow every few
> minutes (`OnUnitActiveSec=3min`) until `/etc/kallon/.enrolled` exists. If the
> first boot attempt fails — Wi-Fi not associated yet, enrollment API briefly
> down, hub SSH hiccup — the tower keeps quietly retrying on its own instead of
> being stuck until someone notices and reboots it. Once enrolled, every tick
> is an instant no-op (`ConditionPathExists` guard), so it's safe to leave
> running forever.

---

## Step 7 — Run acceptance

```bash
sudo scripts/kallon-acceptance.sh --env /etc/kallon/device.env
```

| Check | Pass looks like | If failing |
|-------|-----------------|------------|
| Camera route | `PASS camera 192.168.x.x via enP8p1s0` | Re-run module 60; check `CAMERA_IFACE` |
| Internet route | `PASS internet via wlP1p1s0` | Connect Wi-Fi; fix `WAN_IFACE` |
| No default on camera eth | `PASS enP8p1s0 has no default route` | Re-run module 30 |
| WireGuard | `PASS wg0 present` | Expected WARN pre-enroll — OK |
| RTSP ffprobe | `PASS ffprobe rtsp://127.0.0.1:8554/cam1` | Check `CAMERA_PASSWORD`; `systemctl status mediamtx` |
| HMAC dry-run | `PASS HMAC signature computed` | Confirm `/etc/kallon/alert.key` exists |

Target: **`ACCEPTANCE PASSED`**. A WireGuard WARN is acceptable at this stage — it
becomes a PASS after enrollment.

---

## Step 8 — Enroll the tower

Enrollment happens automatically on the next reboot via `kallon-enroll.service`.
To run it now on the bench (without rebooting):

```bash
sudo scripts/kallon-enroll.sh --env /etc/kallon/device.env
```

What it does:

1. Generates the Jetson WireGuard keypair
2. `POST /v1/enroll` → API validates token, allocates VPN IP, SSH-adds peer on hub
3. Writes `VPN_IP`, `GATEWAY_ENDPOINT`, `GATEWAY_PUBLIC_KEY` into `device.env`
4. Renders `wg0.conf`, brings up `wg0`
5. Waits for WireGuard handshake, then `POST /v1/enroll/confirm`
6. Touches `/etc/kallon/.enrolled` — service is now a permanent no-op

Expected final log: `enrollment complete for kln_<slug>_000001`

| Error | Fix |
|-------|-----|
| `401 invalid enrollment token` | Token mismatch — re-run `register-tower` on control plane and update `ENROLLMENT_TOKEN` |
| `409 hub not provisioned` | Run `python -m registry.cli set-hub --status active` on Artemis |
| Enrollment HTTP fails | Check `ENROLLMENT_URL` and internet connectivity on `WAN_IFACE` |
| No WireGuard handshake | Check enrollment API's log file (`C:\kallon\logs\enrollment-api.log` or `/var/log/kallon/enrollment-api.log`) for the `add_peer` line for this device — see `docs/postgres-windows-server-setup.md` §7.4 "Viewing logs" |

If enrollment fails on the bench run above, don't manually retry in a loop —
`kallon-enroll.timer` (once module 75 is installed) will keep retrying the
whole flow every 3 minutes on its own. Watch it with:

```bash
journalctl -u kallon-enroll.service -f
systemctl list-timers kallon-enroll.timer
```

Verify registry state (on Artemis):

```powershell
python -m registry.cli get-config --device kln_<slug>_000001
# Expected: "status": "active", "vpn_ip": "10.5x.0.x"
```

---

## Step 9 — Final verification

**RTSP over VPN** (from a NOC peer with WireGuard):

```powershell
# Windows NOC peer
Test-NetConnection 10.5x.0.x -Port 8554
ffprobe -rtsp_transport tcp rtsp://10.5x.0.x:8554/cam1
```

**Alerts** (trigger tamper sensor or dry-run):

```bash
# Jetson — watch watchdog
journalctl -u kallon-watchdog -f

# Hub — watch listener
journalctl -u kallon-alert-listener -f
```

Expected: `ALERT ok device=kln_…` on the hub. HTTP 401 means `alert.key` mismatch —
copy the hub's key to the tower and restart `kallon-watchdog`.

---

## Re-flashing a tower

If you re-flash the OS image, the enrollment marker and WireGuard keys are wiped.
Repeat Steps 2–8. When you get to Step 8:

- The old WG peer on the hub will be overwritten (add-peer is idempotent)
- The enrollment token in `device.env` is still valid (it's one-time per device, stored as a hash)
- Remove the old marker if re-testing: `sudo rm -f /etc/kallon/.enrolled`

---

## Chromium troubleshooting (snap 2.70+ issue)

On recent JetPack images, Chromium may fail to launch after a `snapd` update with:

```
cannot set capabilities: Operation not permitted
```

**Quickest fix — downgrade snapd:**

```bash
snap download snapd --revision=24724
sudo snap ack snapd_24724.assert
sudo snap install snapd_24724.snap
sudo snap refresh --hold snapd
sudo systemctl restart snapd.socket snapd.service
```

**If the above doesn't work — use Flatpak instead:**

```bash
sudo apt update && sudo apt install -y flatpak
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
sudo reboot
# After reboot:
flatpak install -y flathub org.chromium.Chromium
flatpak run org.chromium.Chromium
```

The kiosk launcher script (`scripts/install/85-tower-dashboard.sh`) will also
detect `chromium` and `google-chrome` in addition to `chromium-browser` — so a
Flatpak wrapper at any of those names works.

---

## Quick checklist

```
[ ] JetPack image flashed and OS set up
[ ] Repo cloned on Jetson (field-test branch)
[ ] device.env copied + DEVICE_ID / ENROLLMENT_TOKEN / CAMERA_PASSWORD set
[ ] alert.key copied (matches the hub)
[ ] kallon-jetson-install.sh ran without module failures
[ ] kallon-enroll.service enabled
[ ] kallon-enroll.timer enabled (auto-retries until enrolled)
[ ] kallon-acceptance.sh → ACCEPTANCE PASSED
[ ] kallon-enroll.sh ran → enrollment complete
[ ] Registry shows status=active, vpn_ip allocated
[ ] RTSP reachable over VPN from NOC peer
[ ] Alerts HMAC-verified on hub
```

---

*Terra Industries · July 2026*
