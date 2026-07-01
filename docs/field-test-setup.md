# Kallon field-test — Setup & Test Guide

**Terra Industries · Internal Engineering · branch `field-test`**

This is the **end-to-end walkthrough** for the new productization stack: modular
Jetson installer, Postgres/SQLite registry, enrollment API, hub provisioner, and
the RTSP + signed-webhook integration surface.

| Related doc | Role |
|-------------|------|
| **`docs/README.md`** | Documentation index |
| **`docs/architecture-setup-guide.md`** | **Layered setup walkthrough** — nodes, diagrams, commands → resources |
| **`docs/postgres-windows-server-setup.md`** | **Production control plane** — Postgres + enrollment API + automated peer-add |
| **`docs/order-fulfillment.md`** | **Per-order automation** — `kallon-fulfill-order` (hub + towers + device.env) |
| `planning/work-plan.md` | Living task board — what's done vs hardware-gated |
| `planning/mass-deployment-roadmap.md` | Architecture reference (Phases 1–4) |
| `legacy/bench-snapshot-2025-05.md` | May 2025 bench snapshot (historical IPs; re-probe live state) |
| `docs/alert-webhook.md` | Dashboard integration contract |
| `docs/identity-and-secrets.md` | ID formats and secret handling |
| `docs/dev-onvif-ptz.md` | ONVIF/PTZ CLI reference |

> **Replaces for new installs:** the old `deploy/install-kallon-watchdog.sh` +
> hand-edited `wg0.conf` path. Use `scripts/kallon-jetson-install.sh` instead.

---

## 0. What you are building

```mermaid
flowchart TB
    subgraph CP["Control plane (Windows Server — production)"]
        REG["PostgreSQL registry"]
        API["Enrollment API HTTPS"]
        API -->|subprocess| ADDPEER["kallon-gateway-add-peer.sh → hub"]
    end
    subgraph HUB["Customer hub (one VPS per org)"]
        WGH["WireGuard hub :51820"]
        AL["Alert listener :8080"]
    end
    subgraph TOWER["Sentry Tower (Jetson)"]
        INST["kallon-jetson-install.sh"]
        ENR["kallon-enroll.sh"]
        MTX["mediamtx :8554"]
        WD["kallon-watchdog"]
    end
    TOWER -->|HTTPS enroll| API
    API --> REG
    API -->|add peer| HUB
    TOWER <-->|WireGuard| HUB
    WD -->|HMAC POST| AL
    MTX -->|RTSP over wg0| HUB
```

**Recommended build order (production from day 1):**

| Step | Path | Where | What it proves |
|------|------|-------|----------------|
| 1 | **A — Software** | Laptop | Registry, enrollment API, HMAC, peer logic (~5 min) |
| 2 | **P — Production control plane** | **Windows Server** | Postgres + API + TLS + **automated peer-add** (`subprocess`) |
| 3 | **Jetson** | Bench tower | Install, enroll, acceptance, RTSP + alerts over VPN |

| Path | When to use |
|------|-------------|
| **P** | **Default.** Terra control plane on Windows Server; towers hit `https://enroll…/v1`; no manual `wg0.conf` / peer edits. |
| **A** | Quick smoke before touching the server. |
| **B** | **Deprecated** — offline dev only (SQLite + `noop`). Do not use for validation. |

Start with **Path A**, then **Path P** (`docs/postgres-windows-server-setup.md`), then **§5 Jetson tower bring-up**.

**First hub = production:** your existing Lightsail (`18.220.75.237`) is `cust_lab` on
the same architecture every retail customer uses — one Terra ops SSH key, Postgres
registry, automated peer-add. Path P validates operability before you add `cust_acme`.

---

## 1. Prerequisites

### 1.1 Machines & access

| Host | You need | Lab bench example (`legacy/bench-snapshot-2025-05.md`) |
|------|----------|---------------------------------------------|
| **Windows Server** | Postgres 16, Python 3.10+, Git Bash, OpenSSH | **Control plane** — registry + enrollment API (Path P) |
| **Jetson** | SSH as `khalifa`, sudo | `192.168.1.246` (Wi-Fi), repo at `/home/khalifa/kallon` |
| **Hub VPS** | SSH as `ubuntu` with PEM key | `18.220.75.237`, wg0 `10.50.0.1` |
| **Laptop** | Git, Python 3.10+ | Path A tests + SSH to server/Jetson/hub |
| **Camera** | Reachable on camera segment | `192.168.1.108`, user `admin` |

### 1.2 Clone the field-test branch

**On laptop:**

```powershell
cd "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE"
git fetch origin
git checkout field-test
git pull origin field-test
```

| Why | Ensures you have installer, registry, enrollment API, tests |
| Expected | `git branch --show-current` → `field-test` |
| If it fails | `git stash` first if you have local edits; or clone fresh |

**On Jetson:**

```bash
cd /home/khalifa/kallon
git fetch origin
git checkout field-test
git pull origin field-test
```

| Why | Jetson runs scripts from this branch |
| Expected | `ls scripts/kallon-jetson-install.sh` exists |
| If it fails | Check `git remote -v` points at `Yaqcodes/kallon-sentry` |

### 1.3 Python dependencies (laptop / control plane)

```powershell
pip install -r requirements.txt
pip install -r registry/requirements.txt
pip install -r infra/enrollment-api/requirements.txt
pip install fastapi httpx   # for running tests/
```

| Why | Registry CLI, enrollment API, and `tests/*.py` need these |
| Expected | `python -c "import fastapi, psycopg"` succeeds (psycopg optional for SQLite lab) |
| If it fails | Use `python -m pip install ...`; on Jetson use `pip3 install --user` |

---

## 2. Path A — Software tests (no hardware)

Run from the repo root on your laptop.

```powershell
python tests/test_registry.py
python tests/test_enrollment_api.py
python tests/test_alert_hmac.py
python tests/test_e2e_two_tower.py
```

| Test file | What it proves | Expected output |
|-----------|----------------|-----------------|
| `test_registry.py` | Customer/tower CRUD, IP allocation `10.50.0.2`, `10.50.0.3` | `10/10 passed` |
| `test_enrollment_api.py` | Two towers enroll + confirm → `active` | `ALL OK` |
| `test_alert_hmac.py` | Watchdog signing matches hub `verify()` | `ALL OK` |
| `test_e2e_two_tower.py` | Full flow + automated `wg0.conf` with 2 peers, key rotation safe | `ALL OK` |

**Dry-run hub provisioner** (no SSH, no AWS):

```powershell
$env:KALLON_REGISTRY = "sqlite"
$env:KALLON_SQLITE_PATH = "$env:TEMP\kallon_hub_test.db"
python infra/hub-provisioner/cli.py cust_lab --provider manual --host 203.0.113.42 --subnet 10.50.0.0/24 --dry-run
```

| Why | Confirms registry + provisioner CLI wiring without touching the VPS |
| Expected | JSON with `"_dry_run": true`, `"gateway_endpoint": "203.0.113.42:51820"` |
| If it fails | Ensure `field-test` branch; check `python infra/hub-provisioner/cli.py --help` |

---

## 3. Path P — Production control plane (recommended)

Stand up the **Terra control plane on Windows Server** with Postgres, the
enrollment API, TLS, and **automated hub peer-add** (`KALLON_PEER_BACKEND=subprocess`).
Towers never talk to Postgres; they call the enrollment API over HTTPS.

**Follow:** [`docs/postgres-windows-server-setup.md`](postgres-windows-server-setup.md) end-to-end (§1–§12).

| Step | Doc section | Outcome |
|------|-------------|---------|
| Postgres | §1–§5 | Registry on `127.0.0.1`; `init-schema` done |
| Customer + hub | §8 or **fulfill-order** | `cust_*` `status=active`, hub endpoint/pubkey in registry |
| Enrollment API **service** | §7.3–7.4 | Permanent uvicorn + `enrollment-api.env` (not a manual CMD window) |
| TLS | §7 + `Caddyfile.example` | `https://enroll.yourdomain.com/v1` |
| Per-order | **`docs/order-fulfillment.md`** | `kallon-fulfill-order` → `device_*.env` + manifest |
| Verify | §12 | Enroll one tower → peer on hub **without** manual add-peer |

**Per order (recommended — one command):**

```powershell
. .\scripts\load-control-plane.ps1
$env:KALLON_ENROLLMENT_URL = "https://enroll.yourdomain.com/v1"

# Lab: existing Lightsail hub + one tower, two cameras
python infra/fulfillment/cli.py lab --display-name "Kallon Lab" `
  --provider manual --host 18.220.75.237 `
  --towers 1 --cameras 2 --subnet 10.50.0.0/24 `
  --output-dir C:\kallon\factory\lab
```

Writes `device_kln_lab_000001.env` and `fulfillment_cust_lab.json`. Copy env to Jetson, then **§5 — Jetson tower bring-up**.

**Do not** use §5.3 “Add peer on hub” — peer-add is automatic in Path P.

**First hub** = production hub #1 (`cust_lab` on `18.220.75.237`). **New customers** use `--provider lightsail` inside fulfill-order (creates VPS automatically).

---

## 4. Path B — Optional lab shortcut (skip for production)

> **Skip this section** if you are on Path P. Path B uses SQLite on a laptop,
> `KALLON_PEER_BACKEND=noop`, and **manual** peer add — useful only for quick
> experiments without the Windows Server.

Uses **SQLite** on your laptop as the registry, your **existing Lightsail hub**,
and the **Jetson bench**. No Terra control plane required.

### Phase B1 — Registry (SQLite on laptop) *(Path B only)*

```powershell
cd "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE"
$env:KALLON_REGISTRY = "sqlite"
$env:KALLON_SQLITE_PATH = "$env:TEMP\kallon_lab.db"
Remove-Item $env:KALLON_SQLITE_PATH -ErrorAction SilentlyContinue

python -m registry.cli init-schema
```

| Why | Creates `customers`, `towers`, `ip_allocations`, `audit_events` tables |
| Expected | `{"ok": true, "action": "init-schema"}` |
| If it fails | Delete the db file and retry; check Python path is repo root |

**Create customer org** (maps to your existing `10.50.0.0/24` VPN):

```powershell
python -m registry.cli create-customer --slug lab --name "Kallon Lab" --subnet 10.50.0.0/24 --provider manual
```

| Why | One hub per customer org; `cust_lab` owns subnet `10.50.0.0/24` |
| Expected | `"customer_id": "cust_lab"`, `"status": "pending_hub"` |
| If it fails | Subnet already used → pick `10.51.0.0/24` or delete db and start over |

**Register tower** (factory step — save the one-time secrets):

```powershell
python -m registry.cli register-tower --slug lab --serial 1
```

| Why | Pre-registers `kln_lab_000001` with claim code + enrollment token hash |
| Expected | JSON includes `_enrollment_token_PLAINTEXT` and `_claim_code` — **copy both immediately** |
| If it fails | Customer must exist first (`create-customer`) |

> **Important:** The plaintext enrollment token is shown **once**. Paste it into
> `/etc/kallon/device.env` on the Jetson as `ENROLLMENT_TOKEN=...`. Never commit it.

**Point registry at your live hub** (use values from `legacy/bench-snapshot-2025-05.md` §6 or your live hub):

```powershell
python -m registry.cli set-hub --customer cust_lab `
  --endpoint "18.220.75.237:51820" `
  --pubkey "<GATEWAY_PUBLIC_KEY from VPS wg show>" `
  --alert-url "http://10.50.0.1:8080/alerts" `
  --host-id "18.220.75.237" `
  --status active
```

| Why | Enrollment API refuses enroll until `status=active` and hub endpoint/pubkey are set |
| Expected | `"status": "active"`, `"gateway_endpoint": "18.220.75.237:51820"` |
| If it fails | Get pubkey: `ssh ubuntu@18.220.75.237 'sudo wg show wg0 public-key'` |

### Phase B2 — Enrollment API (laptop) *(Path B only — not production)*

**Terminal 1** — keep this running:

```powershell
cd infra/enrollment-api
$env:KALLON_REGISTRY = "sqlite"
$env:KALLON_SQLITE_PATH = "$env:TEMP\kallon_lab.db"
$env:KALLON_PEER_BACKEND = "noop"
# Optional: auto-add peers on hub (needs SSH from laptop to VPS):
# $env:KALLON_PEER_BACKEND = "subprocess"
# $env:KALLON_ADDPEER_CMD = 'ssh ubuntu@18.220.75.237 "sudo bash -s" < ../../scripts/kallon-gateway-add-peer.sh --gateway-host localhost --pubkey {pubkey} --vpn-ip {vpn_ip} --device-id {device_id}'

python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

| Line | Why |
|------|-----|
| `KALLON_REGISTRY=sqlite` | Same DB file as registry CLI |
| `KALLON_PEER_BACKEND=noop` | **Path B only.** Skips peer-add; you add peers manually (§5.3). **Production uses `subprocess`** (`postgres-windows-server-setup.md` §7). |
| `--host 0.0.0.0` | Jetson must reach your laptop IP (not just 127.0.0.1) |
| port `8000` | Matches `ENROLLMENT_URL=...:8000/v1` on Jetson |

| Expected | `Uvicorn running on http://0.0.0.0:8000` |
| Verify | `curl http://localhost:8000/healthz` → `{"status":"ok"}` |
| If Jetson can't reach API | Use laptop LAN IP in `ENROLLMENT_URL`; check Windows firewall allows :8000 |

---

## 5. Jetson tower bring-up (Path P — required)

Factory install and first-boot enrollment on the Jetson. **Path P:** complete §3 on the
Windows Server first, then work through §5.1–5.5 here. **Path B:** complete §4.1–4.2
on the laptop first, then the same Jetson steps (with laptop `ENROLLMENT_URL`).

> **Path P:** `ENROLLMENT_URL=https://enroll.yourdomain.com/v1` and tokens from
> `register-tower` on the Windows Server (§3). **Do not** manually add WireGuard peers
> (§5.3) — the enrollment API does that via `KALLON_PEER_BACKEND=subprocess`.

### 5.1 Jetson `device.env` (bench profile)

SSH to the Jetson and create config from the template.

```bash
sudo install -d -m 0750 -o root -g khalifa /etc/kallon
sudo cp deploy/device.env.example /etc/kallon/device.env
sudo chown root:khalifa /etc/kallon/device.env
sudo chmod 0640 /etc/kallon/device.env
sudoedit /etc/kallon/device.env
```

**Bench-specific values** (direct camera cable, Wi-Fi WAN — matches your live stack):

```bash
# Identity — from registry register-tower output
DEVICE_ID=kln_lab_000001
CUSTOMER_ID=cust_lab
CLAIM_CODE=clm_<paste from register-tower>

# WAN — Wi-Fi primary (your bench uses Wi-Fi for internet, not eth)
WAN_MODE=wifi
WAN_IFACE=wlP1p1s0
WAN_FALLBACK_IFACE=          # leave empty until LTE modem fitted
WAN_METRIC=100
WAN_FALLBACK_METRIC=700

# Camera — direct cable bench (NOT production VLAN 10 yet)
CAMERA_IFACE=enP8p1s0
CAMERA_SUBNET=192.168.1.0/24
CAMERA_JETSON_IP=192.168.1.10/32
CAMERA_IPS=192.168.1.108
CAMERA_RTSP_USER=admin
CAMERA_PASSWORD=<your camera password>
CAMERA_RTSP_PATH=/cam/realmonitor?channel=1&subtype=1

# WireGuard — filled by enrollment OR pre-set for manual bench
VPN_IP=10.50.0.2/32
GATEWAY_ENDPOINT=18.220.75.237:51820
GATEWAY_PUBLIC_KEY=<hub public key>
VPN_SUBNET=10.50.0.0/24

# Enrollment — Path P: HTTPS on Windows Server. Path B: laptop LAN IP :8000
ENROLLMENT_URL=https://enroll.yourdomain.com/v1
# ENROLLMENT_URL=http://192.168.1.230:8000/v1   # Path B only
ENROLLMENT_TOKEN=enr_<paste from register-tower>

# Alerts — must match hub /etc/kallon/alert.key
ALERT_WEBHOOK_URL=http://10.50.0.1:8080/alerts
ALERT_KEY_PATH=/etc/kallon/alert.key

RTSP_URLS=rtsp://127.0.0.1:8554/cam1
```

| Field | Why this value (bench) |
|-------|------------------------|
| `WAN_IFACE=wlP1p1s0` | Ethernet has no internet on bench; Wi-Fi carries SSH/WG/enroll |
| `CAMERA_JETSON_IP=192.168.1.10/32` | Matches live `kallon-camera-route.service` (not /24 — avoids hijacking Wi-Fi LAN) |
| `CAMERA_IPS=192.168.1.108` | Your Dahua camera |
| `ENROLLMENT_URL` | **Path B:** laptop LAN IP `:8000/v1`. **Path P:** `https://enroll.yourdomain.com/v1` |

| Expected after edit | `grep DEVICE_ID /etc/kallon/device.env` shows `kln_lab_000001` |
| If camera unreachable | `ping 192.168.1.108`; check cable on `enP8p1s0`; run `ip route get 192.168.1.108` → must show `enP8p1s0` |

**Sync alert key with hub** (must be identical on tower + hub):

```bash
# On VPS — read existing key
ssh ubuntu@18.220.75.237 'sudo cat /etc/kallon/alert.key'

# On Jetson — write the same value
sudoedit /etc/kallon/alert.key
sudo chown root:khalifa /etc/kallon/alert.key
sudo chmod 0640 /etc/kallon/alert.key
```

| Why | HMAC verification fails if keys differ (`tests/test_alert_hmac.py` contract) |
| Expected | Watchdog alerts return HTTP 200 on hub |
| If 401 on alerts | Re-copy key; restart `kallon-watchdog` and hub listener |

---

### 5.2 Jetson install (full orchestrator)

```bash
cd /home/khalifa/kallon
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env
```

This runs modules **00 → 99** in order. You can run individually:

```bash
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --only-module 30
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --list
```

#### Module reference

| Module | What it does | Expected outcome | Common fixes |
|--------|--------------|------------------|--------------|
| **00-preflight** | Validates arm64, reads `device.env`, checks ID formats | `preflight passed for kln_lab_000001` | Fix `DEVICE_ID` format: `kln_<slug>_<6 digits>` |
| **10-packages** | `apt install` wireguard-tools, ffmpeg, iptables, pip deps | `packages installed` | `sudo apt update` first; need network on Wi-Fi |
| **20-users-groups** | Adds `khalifa` to gpio/i2c/video; scoped sudoers | `added khalifa to gpio` (or already in group) | Group missing on desktop image → WARN is OK |
| **30-network-policy** | Wi-Fi default route, camera eth no gateway, boot assertions | `ASSERT ok: 192.168.1.108 via enP8p1s0` | Wrong `WAN_IFACE`: run `ip link` and fix; Wi-Fi not connected |
| **40-wireguard** | Userspace WG drop-in (Tegra), enable `wg-quick@wg0`, WG watchdog timer | `wg-quick@wg0 up` OR warn if no `wg0.conf` yet | Run `kallon-wg-provision.sh` first if enrolling manually |
| **50-mediamtx** | Downloads pinned mediamtx, renders `/etc/mediamtx.yml` from `CAMERA_IPS` | `rendered /etc/mediamtx.yml for 1 camera(s)` | Wrong `CAMERA_PASSWORD`; camera offline |
| **60-camera-route** | Systemd oneshot pins each camera IP to `CAMERA_IFACE` | `rendered kallon-camera-route.service` | Use `/32` not `/24` on bench to avoid Wi-Fi conflict |
| **70-app** | Copies app to `/opt/kallon`, `pip install -r requirements.txt` | `app installed to /opt/kallon` | Repo path wrong; set `REPO_DIR=/home/khalifa/kallon` |
| **80-watchdogs** | Watchdog + PTZ systemd units, generates `alert.key` if missing | `rendered kallon-watchdog.service` | If auto-generated key: copy to hub before testing alerts |
| **90-firewall** | iptables: TCP 8554 only on `lo` + `wg0` | `firewall rules applied` | SSH still works (rules don't block WAN SSH) |
| **99-acceptance** | Calls `kallon-acceptance.sh` | See §5.4 below | Fix individual failures first |

**WireGuard keys** (if not enrolling yet):

```bash
sudo scripts/kallon-wg-provision.sh --env /etc/kallon/device.env
```

| Why | Generates `/etc/wireguard/jetson.private` + renders `wg0.conf` from `device.env` |
| Expected | Prints Jetson public key to register on hub |
| If rotate needed | Add `--regenerate-keys` then re-add peer on hub |

---

### 5.3 Enrollment (Jetson → API → hub)

**Remove old enrolled marker** if re-testing:

```bash
sudo rm -f /etc/kallon/.enrolled
```

**Run enrollment:**

```bash
sudo scripts/kallon-enroll.sh --env /etc/kallon/device.env
```

| Step | Why | Expected |
|------|-----|----------|
| 1 | `kallon-wg-provision.sh --print-pubkey` | Reads or creates keypair |
| 2 | `POST /v1/enroll` | API returns `vpn_ip`, `gateway_*`, `confirm_token` |
| 3 | Updates `device.env` with hub fields | `VPN_IP`, `GATEWAY_*`, `ALERT_WEBHOOK_URL` written |
| 4 | Renders `wg0.conf`, starts `wg-quick@wg0` | `wg show wg0` shows interface |
| 5 | `POST /v1/enroll/confirm` | Tower status → `active` in registry |
| 6 | Touches `/etc/kallon/.enrolled` | Re-runs are no-ops |

| Expected final log | `enrollment complete for kln_lab_000001` |
| If enroll HTTP fails | Laptop API not reachable → ping laptop IP; check `ENROLLMENT_URL` |
| If `401 invalid enrollment token` | Token mismatch → re-run `register-tower` or fix `ENROLLMENT_TOKEN` |
| If `409 hub not provisioned` | Run `set-hub --status active` on registry |
| If no WG handshake | **Path P:** check enrollment API logs for `add_peer` / SSH failure. **Path B:** add peer manually (below). |

#### Manual peer add *(Path B only — skip on Path P)*

Required only when the enrollment API runs with `KALLON_PEER_BACKEND=noop` (§4.2).
**Path P never needs this** — `subprocess` adds the peer during `POST /v1/enroll`.

```bash
# From laptop or Jetson — substitute PUBKEY from enroll output or:
PUBKEY=$(sudo wg show wg0 public-key)

ssh ubuntu@18.220.75.237 "sudo bash -s" <<EOF
$(cat scripts/kallon-gateway-add-peer.sh)
EOF
# Or run locally on VPS after copying the script:
scripts/kallon-gateway-add-peer.sh \
  --gateway-host localhost \
  --pubkey "$PUBKEY" \
  --vpn-ip 10.50.0.2/32 \
  --device-id kln_lab_000001
```

| Why | Hub must know Jetson public key + allowed-ips `/32` |
| Expected | `OK: peer kln_lab_000001 -> 10.50.0.2/32` |
| If handshake still stale | `sudo systemctl restart wg-quick@wg0` on both sides; check `PersistentKeepalive=25` |

**Enable boot-time enroll** (recommended for production):

```bash
# Prefer installer module (renders unit with repo path + bash ExecStart):
sudo bash scripts/kallon-jetson-install.sh --only-module 75 --env /etc/kallon/device.env

# Or copy the example and fix the path if your repo is not /home/khalifa/kallon:
# sudo cp deploy/kallon-enroll.service.example /etc/systemd/system/kallon-enroll.service
```

| Why | One-shot on first boot; skipped once `.enrolled` exists |
| Expected | Runs once at boot if not yet enrolled |
| `ExecStart` | Must use `/usr/bin/bash …/kallon-enroll.sh` — direct exec fails with `203/EXEC` (scripts are 644 in git) |

---

### 5.4 Acceptance gate

```bash
sudo scripts/kallon-acceptance.sh --env /etc/kallon/device.env
```

| Check | Pass looks like | Fail → fix |
|-------|-----------------|------------|
| Camera route | `PASS camera 192.168.1.108 via enP8p1s0` | Re-run module 60; check `CAMERA_IFACE` |
| Internet route | `PASS internet via wlP1p1s0` | Connect Wi-Fi; fix `WAN_IFACE` |
| No default on eth | `PASS enP8p1s0 has no default route` | Re-run module 30 |
| WireGuard | `PASS wg0 present` + handshake PASS or WARN | **Path P:** FAIL → API peer-add / hub SSH. **Path B:** WARN ok before manual peer |
| RTSP ffprobe | `PASS ffprobe rtsp://127.0.0.1:8554/cam1` | Camera password; `systemctl status mediamtx` |
| HMAC dry-run | `PASS HMAC signature computed` | Create `/etc/kallon/alert.key` |

| Expected finale | `ACCEPTANCE PASSED` (exit 0) |
| Soft WARN on handshake | OK during setup; must be PASS before pilot sign-off |

---

### 5.5 End-to-end verification

**RTSP over VPN** (from NOC PC with WireGuard peer — **not** from the hub shell alone):

```powershell
# Windows NOC peer (e.g. 10.50.0.10) — port check first
Test-NetConnection 10.50.0.2 -Port 8554
ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1
```

| Why | Proves mediamtx rebroadcast reaches **peer-to-peer** VPN consumers (NOC → hub → tower) |
| Expected | `TcpTestSucceeded : True`; stream info printed (codec h264/hevc) |
| If ping OK but TCP 8554 fails | Hub missing `ufw route allow in on wg0 out on wg0` — run `kallon-gateway-ensure-forwarding.sh` **on the hub** (`docs/postgres-windows-server-setup.md` §8.1) |
| If timeout after port open | Jetson `wg0` down; tower module 90 iptables; peer not added |

**Alert over VPN** (trigger tamper or dry-run):

```bash
# On Jetson — watch logs
journalctl -u kallon-watchdog -f

# On VPS — watch listener
journalctl -u kallon-alert-listener -f
# or if still using nohup listener from bench:
# tail -f the process log
```

| Why | Proves HMAC path matches `docs/alert-webhook.md` |
| Expected | Hub logs `ALERT ok device=kln_lab_000001 type=...` |
| If HTTP 401 | Alert key mismatch between Jetson and hub |

**Registry state** (on Windows Server — Path P):

```powershell
$env:KALLON_REGISTRY = "postgres"
$env:DATABASE_URL = "postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon"
python -m registry.cli list-towers --customer cust_lab
python -m registry.cli get-config --device kln_lab_000001
```

| Expected | `"status": "active"`, `"vpn_ip": "10.50.0.2"` |

---

## 6. Linux control plane (alternate to Path P)

**Windows Server is the default production path** — see §3 and
`docs/postgres-windows-server-setup.md`. Use this section only if the control
plane runs on **Linux** instead.

### 6.1 Postgres + enrollment API (Linux)

```bash
# On Terra Linux server
sudo apt install postgresql-16
sudo -u postgres createuser kallon -P
sudo -u postgres createdb kallon -O kallon

export DATABASE_URL="postgresql://kallon:<password>@127.0.0.1:5432/kallon"
export KALLON_REGISTRY=postgres
cd /opt/kallon && python -m registry.cli init-schema
```

Enrollment API env (**always `subprocess` in production**):

```bash
sudo tee /etc/kallon/enrollment-api.env <<EOF
DATABASE_URL=postgresql://kallon:<password>@127.0.0.1:5432/kallon
KALLON_REGISTRY=postgres
KALLON_PEER_BACKEND=subprocess
KALLON_ADDPEER_CMD=/opt/kallon/scripts/kallon-gateway-add-peer.sh --gateway-host {gateway_host} --pubkey {pubkey} --vpn-ip {vpn_ip} --device-id {device_id}
EOF
```

Install `kallon-enrollment-api.service.example`, TLS via `Caddyfile.example` →
`https://enroll.yourdomain.com/v1`. Same contracts as Path P on Windows.

### 6.2 Hub via Lightsail API

```bash
export KALLON_REGISTRY=postgres
export DATABASE_URL=...

python infra/hub-provisioner/cli.py cust_acme \
  --provider lightsail --region us-east-2 \
  --subnet 10.51.0.0/24 --display-name "Acme Security"
```

| Why | One command creates VM, opens UDP 51820, runs `kallon-gateway-init.sh` (incl. wg0 peer forwarding), updates registry |
| Expected | `gateway_manifest_*.json` in `manifests/`; customer `status=active` |
| If boto3 error | `pip install boto3`; configure AWS credentials |
| If SSH timeout | Lightsail instance still booting — wait and retry init |

See `docs/postgres-windows-server-setup.md` §8 for Option C (manual host) details.

---

## 7. Production network profile (managed switch)

When you move from bench cable to production VLAN:

```bash
# In /etc/kallon/device.env — production values
WAN_IFACE=wlP1p1s0
WAN_FALLBACK_IFACE=usb0
CAMERA_IFACE=enP8p1s0
CAMERA_SUBNET=192.168.10.0/24
CAMERA_JETSON_IP=192.168.10.2/24
CAMERA_IPS=192.168.10.108,192.168.10.109
```

| Change | Why |
|--------|-----|
| Camera subnet `192.168.10.0/24` | Isolated VLAN on managed PoE switch |
| Jetson eth `.2/24` | On-camera VLAN; **no default gateway** on eth |
| LTE `usb0` fallback | Higher metric than Wi-Fi; only used when Wi-Fi drops |

Re-run installer module 30 + 60 after changing `device.env`:

```bash
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --only-module 30,60
sudo scripts/kallon-acceptance.sh
```

---

## 8. PTZ benchmark (pilot sign-off)

```bash
python3 scripts/kallon-ptz-benchmark.py --count 1000
```

| Why | Phase 4 exit: document p95 vs 100 ms SLA or re-baseline for ONVIF ceiling |
| Expected | Table with p50/p95/p99; `RESULT: PASS` or `OVER SLA` (both are valid findings) |
| If connection refused | `systemctl status kallon-ptz-daemon`; daemon listens `127.0.0.1:8765` |
| Ping-only baseline | `--method ping` isolates daemon overhead without moving camera |

---

## 9. Troubleshooting quick reference

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ip route get 192.168.1.108` → `wlP1p1s0` | Camera traffic leaking to Wi-Fi | Use `/32` on `CAMERA_JETSON_IP`; re-run module 60 |
| SSH drops after install | Default route moved to camera eth | Never set gateway on `CAMERA_IFACE`; re-run module 30 |
| `wg-quick@wg0` fails on Jetson | Missing userspace drop-in on Tegra | Module 40 installs `wg-quick-wg0-userspace.conf.example` |
| mediamtx up, ffprobe fails | Wrong camera password or path | Check `CAMERA_PASSWORD`, `CAMERA_RTSP_PATH`; test `ffprobe rtsp://admin:pass@192.168.1.108:554/...` |
| Enroll `connection refused` | API not reachable from Jetson | **Path P:** DNS/TLS/firewall on `enroll.*`; `curl` from Jetson. **Path B:** laptop IP :8000 |
| Enroll `401` | Bad enrollment token | Re-register tower on server; update `ENROLLMENT_TOKEN` |
| Enroll `409` | Hub not active in registry | `set-hub --status active` with endpoint + pubkey |
| WG up, no handshake (Path P) | Peer-add failed on server | API logs; `ssh ubuntu@hub` from server; check `KALLON_PEER_BACKEND=subprocess` |
| WG up, no handshake (Path B) | `noop` peer backend | Manual `kallon-gateway-add-peer.sh` (§5.3); check UDP 51820 |
| Alerts `401` | HMAC key mismatch | Sync `/etc/kallon/alert.key` tower ↔ hub |
| RTSP works locally, not over VPN from NOC | Hub UFW blocks `wg0→wg0` FORWARD; or tower iptables / peer | Hub: `kallon-gateway-ensure-forwarding.sh` (hub VPS only). Tower: module 90; `wg show` |
| `apt install` fails on Jetson | No internet on Wi-Fi | `ping 1.1.1.1`; fix Wi-Fi before module 10 |

---

## 10. Two-tower test (second unit)

On the **Windows Server** (Path P), repeat `register-tower` with `--serial 2`, install a second Jetson (or
re-flash SD), enroll both against the same `cust_lab` hub:

```powershell
python -m registry.cli register-tower --slug lab --serial 2
```

| Expected | Tower 2 gets `vpn_ip` `10.50.0.3`; hub `wg0.conf` has two `[Peer]` blocks |
| Software proof | `python tests/test_e2e_two_tower.py` (already in CI path) |
| Live proof | `ffprobe rtsp://10.50.0.2:8554/cam1` and `rtsp://10.50.0.3:8554/cam1` from NOC |

---

## 11. What success looks like (checklist)

**Control plane (Path P):**

- [ ] Path A tests all green on laptop
- [ ] Postgres on Windows Server; `listen_addresses = localhost`
- [ ] Enrollment API with `KALLON_PEER_BACKEND=subprocess`; SSH to hub works
- [ ] TLS: `https://enroll.yourdomain.com/healthz` OK
- [ ] `cust_lab` customer `active` in registry with hub endpoint/pubkey
- [ ] Hub UFW: `ufw route allow in on wg0 out on wg0` (new hubs via `gateway-init`; legacy: `kallon-gateway-ensure-forwarding.sh` on hub)

**Tower (Jetson):**

- [ ] `register-tower` on server; `ENROLLMENT_TOKEN` baked in `device.env`
- [ ] `kallon-jetson-install.sh` completes without module failure
- [ ] `kallon-enroll.service` enabled (or `kallon-enroll.sh` succeeded once)
- [ ] `kln_lab_000001` tower `active`, `vpn_ip` allocated — **no manual add-peer**
- [ ] `kallon-acceptance.sh` → `ACCEPTANCE PASSED`
- [ ] `wg show wg0` handshake &lt; 180 s on Jetson and hub
- [ ] `ffprobe rtsp://10.50.0.2:8554/cam1` works from **NOC VPN peer** (`Test-NetConnection` port 8554 succeeds)
- [ ] Watchdog alert → hub HTTP 200
- [ ] No hand-edited `wg0.conf`

---

## 12. Document map (what to read when)

| I want to… | Read |
|------------|------|
| Stand up **production control plane** (Windows Server) | **`docs/postgres-windows-server-setup.md`** |
| **End-to-end** flow (Paths A / P / Jetson) | **This doc** |
| See what's **already running** on bench | `legacy/bench-snapshot-2025-05.md` (May 2025 — re-probe for current) |
| Understand **architecture / phases** | `planning/mass-deployment-roadmap.md` |
| Provision a **new customer hub** | `docs/postgres-windows-server-setup.md` §8 + **§8.1** (peer forwarding) |
| Wire the **dashboard** | `docs/alert-webhook.md` |
| **ONVIF/PTZ CLI** | `docs/dev-onvif-ptz.md` |
| Track **remaining work** | `planning/work-plan.md` |

---

*Keep in sync with `field-test` branch. Report doc gaps in `planning/work-plan.md`.*
