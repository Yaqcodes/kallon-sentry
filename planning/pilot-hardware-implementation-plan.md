# Kallon Pilot — Hardware Bring-Up Walkthrough

**Terra Industries · Internal Engineering · June 2026**

This is a step-by-step guide for bringing up a Kallon Sentry Tower on the production
network profile. Follow the steps in order on a single engineer's bench.

**Hardware assumed present:**

- Jetson Orin Nano (or NX) — booting from SD (NVMe boot not required)
- NVMe SSD — mounted as a **data volume** (`/var/kallon/recordings`), OS stays on SD
- TP-Link SG2210P managed PoE+ switch
- Small Wi-Fi AP (dedicated WAN SSID for the tower)
- Dahua (or compatible ONVIF) IP camera
- Laptop for Omada/Wireshark; Windows Server (Artemis) as control plane

**Branch:** `field-test` on both laptop/server and Jetson.

| Reference doc | When to open |
|---------------|--------------|
| [`docs/field-test-setup.md`](../docs/field-test-setup.md) | Jetson installer + enroll detail |
| [`docs/postgres-windows-server-setup.md`](../docs/postgres-windows-server-setup.md) | Control plane (Steps 5 + 6) |
| [`docs/alert-webhook.md`](../docs/alert-webhook.md) | Dashboard integration contract |
| [`planning/work-plan.md`](work-plan.md) | Task board — update `[H]` checkboxes as steps complete |

---

## Before you start

Collect these values and keep them handy. You will fill them into `device.env` in Step 6.

| Value | Where to get it | Your value |
|-------|-----------------|------------|
| Camera admin password | Camera web UI | |
| Wi-Fi AP SSID + password | AP config | |
| Hub VPS IP | `ssh ubuntu@<vps>` | `18.220.75.237` |
| Hub WireGuard public key | `sudo wg show wg0 public-key` on hub | |
| `ENROLLMENT_URL` | Set in Step 5 | `https://enroll.<your-domain>/v1` |
| `ENROLLMENT_TOKEN` | Output of `register-tower` in Step 5 | |
| `DEVICE_ID` | Output of `register-tower` | |
| `CLAIM_CODE` | Output of `register-tower` | |

---

## Step 1 — Wi-Fi WAN on the Jetson

**What this does:** gives the Jetson internet access on `wlP1p1s0`. This must come first —
`apt`, enrollment, and WireGuard all depend on it.

1. Configure your small AP: fixed SSID, WPA2/3, DHCP enabled, no bridge to the camera segment.
2. On the Jetson, connect to that SSID:

```bash
sudo nmcli dev wifi connect "<YOUR-SSID>" password "<YOUR-WIFI-PASSWORD>"
```

3. Confirm:

```bash
ip route get 1.1.1.1
# expected: ... dev wlP1p1s0 ...

ping -c 3 1.1.1.1
# expected: 0% packet loss
```

4. Note the Jetson Wi-Fi IP (use this for all SSH sessions):

```bash
ip -brief addr show wlP1p1s0
```

**Done when:** `ping 1.1.1.1` succeeds and `ip route get 1.1.1.1` shows `wlP1p1s0`.

---

## Step 2 — Mount NVMe as recordings volume

**What this does:** puts the Jetson's NVMe SSD at `/var/kallon/recordings` so mediamtx
can write to it. The OS stays on SD. SMART health monitoring (`ENABLE_NVME=1`) also
activates once the drive is present.

> **Disk sizing:** substream at ~1 Mbps = ~450 MB/hour = ~11 GB/day per camera.
> Allow at least 20 GB per camera-day plus headroom. A 100 GB partition comfortably
> holds 1 camera × 4 days.

1. Power off the Jetson, install the M.2 2280 NVMe, power on.

2. Confirm the drive is visible:

```bash
lsblk
# expected: nvme0n1 listed
```

3. Partition and format (skip if already formatted):

```bash
sudo parted /dev/nvme0n1 mklabel gpt
sudo parted /dev/nvme0n1 mkpart recordings ext4 0% 100%
sudo mkfs.ext4 /dev/nvme0n1p1 -L kallon-rec
```

4. Create the mount point and add to `/etc/fstab`:

```bash
sudo mkdir -p /var/kallon/recordings

# Get the UUID
sudo blkid /dev/nvme0n1p1
# output: UUID="xxxxxxxx-xxxx-..."

# Add to fstab (replace UUID with your value)
echo 'UUID=<YOUR-UUID>  /var/kallon/recordings  ext4  defaults,noatime  0  2' \
  | sudo tee -a /etc/fstab

sudo mount -a
```

5. Confirm it's mounted as a separate filesystem:

```bash
mountpoint /var/kallon/recordings
# expected: /var/kallon/recordings is a mountpoint

df -h /var/kallon/recordings
# expected: shows nvme0n1p1, not the SD root
```

6. Install SMART tools:

```bash
sudo apt install -y smartmontools
sudo smartctl -a /dev/nvme0
# expected: no critical_warning; no media_errors
```

**Done when:** `mountpoint` confirms the path and `smartctl` reads without errors.

---

## Step 3 — Wire the SG2210P switch

**What this does:** creates a dedicated camera VLAN (VLAN 10) so cameras are isolated from
the internet, with an ACL that permits only camera ↔ Jetson traffic.

### 3.1 Physical wiring and port map

```text
Port 1   Wireshark laptop (mirror destination) — plug in only for Step 11 capture
Port 2   Jetson enP8p1s0                       — VLAN 10, 192.168.10.2/24
Port 3   Camera 1 (PoE)                         — VLAN 10, 192.168.10.108
Port 4   Camera 2 (PoE, future)                 — VLAN 10, 192.168.10.109
Port 5   Camera 3 (PoE, future)                 — VLAN 10, 192.168.10.110
Port 6   Camera 4 (PoE, future)                 — VLAN 10, 192.168.10.111
Port 7–8 Spare
```

```text
[Camera(s)] ──PoE── SG2210P ports 3–6  (VLAN 10)
[Jetson eth] ─────  SG2210P port 2     (VLAN 10)
[Mirror PC]  ─────  SG2210P port 1     (receives copy of camera traffic — Step 11 only)
```

Ethernet topology in the enclosure will be: cameras on PoE ports 3–6, one standard port to
the Jetson's `enP8p1s0` NIC on port 2. The Jetson Wi-Fi (`wlP1p1s0`) connects to the AP
separately and is **not** wired through the switch. Internet is **never** via the switch —
only via Jetson Wi-Fi.

### 3.2 Omada: adopt the switch

1. Open Omada Controller (software or mobile app).
2. Adopt the SG2210P (it should appear in "Pending Devices").
3. Apply any firmware update if prompted.

### 3.3 Create VLAN 10 (camera segment)

In Omada → **Settings → Networks**:

| Field | Value |
|-------|-------|
| Name | `CAMERA` |
| VLAN ID | `10` |
| Purpose | No DHCP server — cameras use static IPs |

### 3.4 Assign ports to VLAN 10 (Omada “Access” ports)

**Port map** (physical wiring — which cable goes where):

| Port | Device | PoE |
|------|--------|-----|
| 1 | Mirror laptop (Step 11 only) | off |
| 2 | Jetson `enP8p1s0` | off |
| 3 | Camera 1 | on |
| 4 | Camera 2 (future) | on |
| 5 | Camera 3 (future) | on |
| 6 | Camera 4 (future) | on |
| 7 | PC for adoption / lab (optional) | off |
| 8 | Spare | — |

**Omada v6: “Type” is read-only — you do not pick Access vs Trunk**

On adopted switches, the **Type** column shows **Trunk** on every port until VLAN
tagging is tightened. There is no dropdown to change Type. Omada labels a port **Access**
only when **one** untagged VLAN is allowed (the native / PVID) and **no** tagged VLANs
are carried.

The **Profile** dropdown on **Device Config → Switch → Switch Ports** (All / Default /
Disable) is **not** VLAN mode and will not fix camera reachability.

**Configure each production port (2–6, and 1 for mirror capture)**

1. **Devices → [SG2210P] → Ports** → select port(s) → **Edit** (batch ports 2–6 is fine).
2. Set **Port Configuration** to **Custom** (not “follow profile” with the factory default).
3. Under **VLAN**:
   - **Native Network** → `CAMERA` (VLAN **10**)
   - **Network tag settings** → **Block All**
     (untagged = PVID only; tagged list empty — this is what makes Type flip to **Access**)
4. Enable **PoE** on camera ports 3–6.
5. **Apply** / **Save**. Refresh the port list — **Type** should read **Access**.

**Reusable profile (optional):** **Device Config → Switch → Switch Ports → Port Profile**
→ create e.g. `CAMERA-ACCESS` with Native Network = VLAN 10 and tag settings = **Block All**
→ assign that profile to ports 2–6.

**Verify in Omada:** on the switch **Ports** view, filter or colour-code **VLAN 10** — ports 2
and 3 must be **Native / Untagged** for VLAN 10 and must **not** be **Tagged** on VLAN 1
or other VLANs.

**Adoption PC (port 7 only):** while adopting or troubleshooting the switch, set port 7 to
**Custom → Native Network = Default (VLAN 1) → Block All**, PC at `192.168.0.100/24`. Do
**not** put the Jetson or cameras on VLAN 1. After adoption, either disable port 7 or move
it to VLAN 10 if you need a bench laptop on `192.168.10.x`.

**Standalone UI fallback** (if Omada VLAN options are missing): browser → `http://<switch-ip>`
→ **VLAN → 802.1Q VLAN** → add VLAN 10; set ports 2–6 as **Untagged** members of VLAN 10 only.

**How the Jetson finds cameras (IP vs switch port number)**

The Jetson **never** uses switch port numbers (3, 4, …). It uses **IP addresses** from
`/etc/kallon/device.env`:

- `CAMERA_IFACE=enP8p1s0` — wired NIC plugged into switch **port 2**
- `CAMERA_JETSON_IP=192.168.10.2/24` — Jetson’s address on that segment
- `CAMERA_IPS=192.168.10.108` — camera IP(s); installer pins each `/32` to `CAMERA_IFACE`

So “which port is the Jetson checking?” means: **whatever IP is in `CAMERA_IPS`, reached
via `enP8p1s0`**. The switch must put **port 2 (Jetson)** and **port 3 (camera)** on the
**same untagged VLAN 10** so ARP and RTSP work. Moving the camera to switch port 4 does
not matter as long as that port is also Access VLAN 10 and the camera keeps `192.168.10.108`.

```bash
# On Jetson — routing (Layer 3 policy), not switch port numbers:
ip route get 192.168.10.108   # must show: dev enP8p1s0
ip addr show enP8p1s0         # must show: 192.168.10.2/24
grep CAMERA_IPS /etc/kallon/device.env
```

**Done when:** Omada shows **Access** on ports 2–6, `ping -c 3 192.168.10.108` from the
Jetson succeeds, and `ip neigh show dev enP8p1s0` lists `192.168.10.108` as **REACHABLE**
(not `FAILED`).

### 3.5 ACL — cameras cannot reach the internet

Use a **Switch ACL** (not Gateway ACL, not EAP ACL).

Camera ↔ Jetson traffic stays entirely within VLAN 10 on the same L2 segment — it never
crosses a router, so a Gateway ACL would never see it. There is also no Omada gateway
(ER-series) in this topology. Switch ACL is applied by the SG2210P itself.

**Step A — create IP Groups first** (Settings → Profiles → IP Group):

| Group name | Type | Value |
|------------|------|-------|
| `cameras` | Subnet | `192.168.10.0/24` |
| `jetson-eth` | Host | `192.168.10.2/32` |

You need these before you can reference them in ACL rules. Using **IP Group** (not
"Network") lets you pin the destination to a specific host address rather than the
entire VLAN 10 network object.

**Step B — create the Switch ACL** (Settings → ACL → Switch ACL):

1. Create a new policy; bind it to **VLAN 10**.
2. Add rules in this order (first match wins):

| Priority | Source type | Source | Destination type | Destination | Action |
|----------|-------------|--------|------------------|-------------|--------|
| 1 | IP Group | `cameras` | IP Group | `jetson-eth` | **Permit** |
| 2 | IP Group | `jetson-eth` | IP Group | `cameras` | **Permit** |
| 3 | IP Group | `cameras` | Any | Any | **Deny** |

Rules 1–2 allow camera ↔ Jetson traffic (RTSP, ARP replies, ping in both directions).
Rule 3 drops everything else sourced from the camera subnet — no internet, no AP, no other
hosts.

If ping from the Jetson to `.108` still fails with VLAN 10 correct, **temporarily disable
the Switch ACL** to test. Re-enable with rules 1–3 above once L2 works.

### 3.6 Mirror port (for Step 11 zero-egress capture)

The mirror laptop on **port 1** receives a **copy** of traffic from camera ports **3–6**.
It is only needed during the 24 h Wireshark capture in Step 11 — not part of normal operation.

On the SG2210P, port mirroring is most reliably configured via the **switch standalone web
UI** (Omada may not expose mirroring for this model under Device Config → Switch).

1. In Omada **Devices**, note the SG2210P **IP address**.
2. Browser → `http://<switch-ip>` → login.
3. **MAINTENANCE → Mirroring → Edit** (or **Switching → Port Mirror** on older firmware).

| Field | Value |
|-------|-------|
| **Destination port** | Port **1** (Wireshark laptop) |
| **Source port(s)** | Port **3** (add 4, 5, 6 as you add cameras) |
| **Ingress** | ✓ |
| **Egress** | ✓ |

Enable both Ingress and Egress on each source port so you capture all traffic the camera
sends and receives.

**Omada alternative** (if your controller shows it): **Devices → SG2210P → Ports → Edit
port 1** → Profile Overrides → Operation: **Mirroring** → select source ports 3–6.

**Done when:** Omada/standalone UI shows mirror active; laptop on port 1 sees camera MAC
frames in Wireshark when a camera on port 3 is powered.

---

## Step 4 — Camera: static IP and cloud disable

**What this does:** moves the camera to the production address (`192.168.10.108`) and
disables all Dahua cloud egress so the zero-egress capture in Step 11 passes.

1. Connect camera directly to laptop (before wiring to switch) or via a temporary port.

2. Log in to Dahua web UI (default `192.168.1.108:80`, user `admin`).

3. **Network → TCP/IP**: set static IP `192.168.10.108`, mask `255.255.255.0`.
   Leave gateway **blank** (cameras have no internet path).

4. **Disable all cloud features** (exact menu varies by firmware):
   - P2P: off
   - DMSS: off
   - Auto Cloud Update: off
   - UPnP: off

5. Apply and reconnect at the new address. Confirm ONVIF and RTSP still work from a
   host on `192.168.10.x`:

```bash
ffprobe -rtsp_transport tcp \
  "rtsp://admin:<PASSWORD>@192.168.10.108:554/cam/realmonitor?channel=1&subtype=1"
# expected: stream info printed (h264/hevc codec)
```

6. Wire camera to SG2210P **port 3** (PoE). Confirm from Jetson once wired (after Step 7):

```bash
ping -c 3 192.168.10.108   # from Jetson — must reply via enP8p1s0
```

For additional cameras, use ports 4–6 with addresses `192.168.10.109`–`.111` and add each
IP to `CAMERA_IPS` in Step 6.

**Done when:** camera responds at `192.168.10.108`, cloud features off, local RTSP works.

---

## Step 5 — Windows Server: control plane (Path P)

**What this does:** stands up the Terra control plane — Postgres registry, enrollment API,
and TLS — so the Jetson can auto-enroll without any manual WireGuard peer editing.

**Reference:** [`docs/postgres-windows-server-setup.md`](../docs/postgres-windows-server-setup.md) §1–§12

Work through these sub-steps on the Windows Server (Artemis):

### 5.1 PostgreSQL 16

```powershell
# After installing PostgreSQL 16 (postgresql.org/download/windows):
psql -U postgres -h localhost
```

```sql
CREATE USER kallon WITH PASSWORD 'choose-strong-password';
CREATE DATABASE kallon OWNER kallon;
GRANT ALL PRIVILEGES ON DATABASE kallon TO kallon;
\q
```

Lock down `postgresql.conf` (`listen_addresses = 'localhost'`) and `pg_hba.conf`
(no public 5432). Restart the service.

Initialize schema:

```powershell
cd "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE"
$env:KALLON_REGISTRY = "postgres"
$env:DATABASE_URL = "postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon"
python -m registry.cli init-schema
# expected: {"ok": true, "action": "init-schema"}
```

### 5.2 Terra hub-ops SSH key

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-terra-hub-ops-key.ps1 `
  -SourcePem "C:\path\to\kallon-vps-key.pem" `
  -HubHost 18.220.75.237

$env:KALLON_OPS_SSH_IDENTITY_FILE = "C:\kallon\secrets\terra-hub-ops.pem"
$env:KALLON_OPS_SSH_PUBKEY_FILE   = "C:\kallon\secrets\terra-hub-ops.pub"

powershell -ExecutionPolicy Bypass -File .\scripts\kallon-hub-ssh-verify.ps1 -HubHost 18.220.75.237
# expected: Test 1 PASS
```

### 5.3 Register the lab customer + tower (one-command fulfillment)

```powershell
$env:KALLON_ENROLLMENT_URL = "https://enroll.yourdomain.com/v1"

python infra/fulfillment/cli.py lab --display-name "Kallon Lab" `
  --provider manual --host 18.220.75.237 `
  --towers 1 --cameras 1 --subnet 10.50.0.0/24 `
  --output-dir C:\kallon\factory\lab
```

This writes `C:\kallon\factory\lab\device_kln_lab_000001.env`. Open it and copy:

- `DEVICE_ID`, `CUSTOMER_ID`, `CLAIM_CODE`
- `ENROLLMENT_TOKEN` — **shown once, copy immediately**

Verify hub is registered in Postgres:

```powershell
. .\scripts\load-control-plane.ps1
python -m registry.cli list-customers
# expected: cust_lab  status=active  gateway_endpoint=18.220.75.237:51820
```

### 5.4 Enrollment API as a persistent service

Create `C:\kallon\config\enrollment-api.env`:

```ini
KALLON_REGISTRY=postgres
DATABASE_URL=postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon
KALLON_OPS_SSH_PUBKEY_FILE=C:\kallon\secrets\terra-hub-ops.pub
KALLON_OPS_SSH_IDENTITY_FILE=C:\kallon\secrets\terra-hub-ops.pem
KALLON_PEER_BACKEND=subprocess
KALLON_ADDPEER_CMD="C:\Program Files\Git\bin\bash.exe" "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE\scripts\kallon-gateway-add-peer.sh" --gateway-host {gateway_host} --pubkey {pubkey} --vpn-ip {vpn_ip} --device-id {device_id} --ssh-user ubuntu
```

Install with NSSM (run as Administrator):

```powershell
$nssm   = "C:\path\to\nssm.exe"
$python = (Get-Command python).Source
$repo   = "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE"

& $nssm install kallon-enrollment-api $python "-m" "uvicorn" "app.main:app" "--host" "127.0.0.1" "--port" "8000"
& $nssm set kallon-enrollment-api AppDirectory "$repo\infra\enrollment-api"
& $nssm set kallon-enrollment-api AppEnvironmentExtra `
    "KALLON_REGISTRY=postgres" `
    "DATABASE_URL=postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon" `
    "KALLON_PEER_BACKEND=subprocess" `
    "KALLON_OPS_SSH_IDENTITY_FILE=C:\kallon\secrets\terra-hub-ops.pem" `
    "KALLON_OPS_SSH_PUBKEY_FILE=C:\kallon\secrets\terra-hub-ops.pub" `
    "KALLON_ADDPEER_CMD=..."
& $nssm start kallon-enrollment-api

curl http://127.0.0.1:8000/healthz
# expected: {"status":"ok"}
```

### 5.5 TLS reverse proxy

1. DNS: add `A` record `enroll` → server public IP.
2. Windows Firewall: allow inbound TCP 443.
3. Install Caddy (or nginx); use `infra/enrollment-api/deploy/Caddyfile.example` as template,
   replacing `enroll.terra.example` with your real hostname.
4. Test from a phone on LTE (not the same LAN):

```
curl https://enroll.yourdomain.com/healthz
# expected: {"status":"ok"}
```

### 5.6 Schedule daily backup

```powershell
# Manual test first
& "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE\scripts\postgres-backup.cmd"
# expected: C:\kallon\backups\kallon_YYYYMMDD.dump created
```

Then add a daily Task Scheduler job pointing at `postgres-backup.cmd` at 02:00.

**Done when:** `https://enroll.yourdomain.com/healthz` returns OK from the internet and
`cust_lab` shows `status=active` in Postgres.

---

## Step 6 — Write `device.env` on the Jetson

**What this does:** sets the production network profile, enrollment credentials, and
recording config in `/etc/kallon/device.env`. Every subsequent installer module reads
this file.

SSH to the Jetson (use its Wi-Fi IP from Step 1):

```bash
sudo install -d -m 0750 -o root -g khalifa /etc/kallon

# Copy the fulfillment output from the Windows Server
scp C:\kallon\factory\lab\device_kln_lab_000001.env \
  khalifa@<JETSON-WIFI-IP>:/tmp/device.env
# Then on Jetson:
sudo install -m 0640 -o root -g khalifa /tmp/device.env /etc/kallon/device.env
```

Open and set these production-specific values (merge with the identity/enrollment fields
that fulfillment already filled in):

```bash
sudoedit /etc/kallon/device.env
```

```bash
# WAN
WAN_MODE=wifi
WAN_IFACE=wlP1p1s0
WAN_FALLBACK_IFACE=          # leave blank — LTE modem not yet fitted
WAN_METRIC=100

# Camera VLAN (production — matches Step 3 addressing)
CAMERA_IFACE=enP8p1s0
CAMERA_SUBNET=192.168.10.0/24
CAMERA_JETSON_IP=192.168.10.2/24
CAMERA_IPS=192.168.10.108
CAMERA_RTSP_USER=admin
CAMERA_PASSWORD=<camera admin password>
CAMERA_RTSP_PATH=/cam/realmonitor?channel=1&subtype=1

# Enrollment (from Step 5 fulfillment output)
ENROLLMENT_URL=https://enroll.yourdomain.com/v1
ENROLLMENT_TOKEN=enr_<paste from fulfillment>

# Alerts
ALERT_WEBHOOK_URL=http://10.50.0.1:8080/alerts
ALERT_KEY_PATH=/etc/kallon/alert.key

# NVMe health alerts (SSD present from Step 2)
ENABLE_NVME=1
NVME_DEVICE=/dev/nvme0

# Recording (turn on once NVMe is mounted — Step 2 complete)
RECORD_ENABLE=1
RECORD_PATH=/var/kallon/recordings
RECORD_MEDIAMTX_DELETE_AFTER=24h
RECORD_MEDIAMTX_SEGMENT_FILE_DURATION=1h

RTSP_URLS=rtsp://127.0.0.1:8554/cam1
```

> **Retention:** `RECORD_MEDIAMTX_DELETE_AFTER` → mediamtx `recordDeleteAfter` (`24h`, `48h`, `168h` …).
> **Segment file length:** `RECORD_MEDIAMTX_SEGMENT_FILE_DURATION` → `recordSegmentDuration` (default `1h`).
> Part flush (`recordPartDuration`) is fixed at `1s` in the installer — do not confuse with segment file length.

Sync the alert HMAC key with the hub (they must be identical):

```bash
# Read the existing key from the hub
ssh ubuntu@18.220.75.237 'sudo cat /etc/kallon/alert.key'

# Write it on the Jetson
sudoedit /etc/kallon/alert.key
sudo chown root:khalifa /etc/kallon/alert.key
sudo chmod 0640 /etc/kallon/alert.key
```

If the hub has no key yet, module 80 will generate one — copy it to the hub after install.

**Done when:** `grep DEVICE_ID /etc/kallon/device.env` shows `kln_lab_000001` and
`grep RECORD_ENABLE` shows `1`.

---

## Step 7 — Run the full installer

**What this does:** runs all 10 installer modules in order (packages → network policy →
WireGuard → mediamtx with recording → watchdogs → firewall → acceptance).

```bash
cd /home/khalifa/kallon
git checkout field-test && git pull

sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env
```

Each module prints `OK:` on success or `WARN:` for non-fatal issues. Watch for errors
in the network (30) and mediamtx (50) modules in particular.

**Verify key modules individually if something fails:**

```bash
# Network routing
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --only-module 30
# expected: ASSERT ok: 192.168.10.108 via enP8p1s0
# expected: ASSERT ok: 1.1.1.1 via wlP1p1s0

# mediamtx with recording
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --only-module 50
# expected: rendered /etc/mediamtx.yml for 1 camera(s) (recording → /var/kallon/recordings, delete after 24h)
# expected: recording directory ensured: /var/kallon/recordings

# Check rendered config
sudo cat /etc/mediamtx.yml
# expected: record: yes  recordDeleteAfter: 24h  recordSegmentDuration: 1h  recordPartDuration: 1s  sourceOnDemand: no
```

**Critical routing assertions (must pass before proceeding):**

```bash
ip route get 192.168.10.108   # → dev enP8p1s0  (camera via switch)
ip route get 1.1.1.1          # → dev wlP1p1s0  (internet via Wi-Fi)
```

If the camera route points to `wlP1p1s0`, re-run module 30 and 60.

**Done when:** installer exits 0 and `systemctl is-active mediamtx wg-quick@wg0
kallon-watchdog kallon-ptz-daemon` are all active.

---

## Step 8 — Enroll the tower

**What this does:** calls the enrollment API over HTTPS. The API allocates a VPN IP,
SSHes to the hub, adds the WireGuard peer, and writes the active registry row — no
manual peer editing.

```bash
# Remove any previous marker if re-testing
sudo rm -f /etc/kallon/.enrolled

sudo scripts/kallon-enroll.sh --env /etc/kallon/device.env
```

Expected log line: `enrollment complete for kln_lab_000001`

**Verify on the Windows Server:**

```powershell
ssh ubuntu@18.220.75.237 "sudo wg show wg0"
# expected: new peer with allowed-ips 10.50.0.2/32 and recent handshake

. .\scripts\load-control-plane.ps1
python -m registry.cli list-towers --customer cust_lab
# expected: status=active  vpn_ip=10.50.0.2  enrolled_at=<timestamp>
```

**Verify on the Jetson:**

```bash
sudo wg show wg0
# expected: latest handshake: a few seconds ago
```

**Done when:** hub shows the peer, Jetson WG handshake is live, registry row is `active`.

---

## Step 9 — Acceptance gate and live-path verification

**What this does:** runs the acceptance script and then verifies RTSP + HMAC end-to-end
over the VPN.

### 9.1 Acceptance script

```bash
sudo scripts/kallon-acceptance.sh --env /etc/kallon/device.env
```

| Check | Must show for pilot sign-off |
|-------|------------------------------|
| Camera route | `PASS camera 192.168.10.108 via enP8p1s0` |
| Internet route | `PASS internet via wlP1p1s0` |
| No default on eth | `PASS enP8p1s0 has no default route` |
| WireGuard | `PASS` handshake (not just WARN) |
| RTSP | `PASS ffprobe rtsp://127.0.0.1:8554/cam1` |
| HMAC | `PASS HMAC signature computed` |

Final line must be: `ACCEPTANCE PASSED`

### 9.2 RTSP over VPN (NOC / dashboard peer)

**Topology:** towers and operator laptops are **separate WireGuard peers** on the hub.
The hub routes between them (`ip_forward`). `kallon-gateway-init.sh` must allow UFW
**forward** on `wg0 → wg0` (`ufw route allow in on wg0 out on wg0`). Without that rule,
ping to a tower VPN IP may work but **TCP (RTSP :8554) fails** from the NOC peer.

From a host with a WireGuard peer in `10.50.0.0/24` (your NOC laptop):

```powershell
# Windows — port must succeed before VLC/ffprobe
Test-NetConnection 10.50.0.2 -Port 8554
ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1
# VLC: use --rtsp-tcp or :rtsp-tcp in stream options
```

From the hub (sanity check that the tower serves RTSP):

```bash
ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1
```

**Hubs provisioned before the forwarding fix** (one-time, idempotent — run **on the hub VPS**, not the Jetson):

```bash
# On the hub (SSH ubuntu@<hub-public-ip>):
sudo bash scripts/kallon-gateway-ensure-forwarding.sh
```

New hubs get this automatically from `kallon-gateway-init.sh` via hub-provisioner.

### 9.3 HMAC alert over VPN

On the Jetson, trigger a tamper event (open the enclosure / shake the tower) or trip the
reed switch. Then watch both ends:

```bash
# Jetson
journalctl -u kallon-watchdog -f

# Hub (separate terminal)
ssh ubuntu@18.220.75.237 'journalctl -u kallon-alert-listener -f'
# expected: ALERT ok  device=kln_lab_000001  type=TAMPER_DOOR_OPEN (or similar)
```

**Done when:** acceptance passes, RTSP streams over VPN, and an alert returns HTTP 200.

---

## Step 10 — Verify continuous recording

**What this does:** confirms mediamtx is writing fMP4 segments to the NVMe and that
auto-deletion kicks in after the retention window.

### 10.1 Files growing on disk

```bash
ls -lh /var/kallon/recordings/cam1/
# expected: one or more .mp4 files, newest timestamp recent
# files are named: 2026-06-30_12-00-00-000000.mp4 (YYYY-MM-DD_HH-MM-SS-µs)
```

If the directory is empty, check:

```bash
sudo cat /etc/mediamtx.yml | grep -A6 cam1
# must show:  sourceOnDemand: no
#             record: yes
#             recordPath: /var/kallon/recordings/...

journalctl -u mediamtx -f
# look for recording-related log lines; errors will be obvious
```

### 10.2 Confirm source is always connected (not on-demand)

```bash
journalctl -u mediamtx | grep -i "source.*connected\|reading"
# expected: connection logs even when no one is watching the live stream
```

### 10.3 Retention

`recordDeleteAfter: 24h` in mediamtx.yml means segments older than 24 hours are deleted
automatically by mediamtx — no cron needed. To verify, change `RECORD_MEDIAMTX_DELETE_AFTER=1h` in
`device.env`, re-run module 50, wait 65 minutes, and confirm old files are gone. Then
restore `RECORD_MEDIAMTX_DELETE_AFTER=24h`.

### 10.4 Disk space check

```bash
df -h /var/kallon/recordings
watch -n 60 "du -sh /var/kallon/recordings/cam1/"
# grows at ~450 MB/hour per camera at 1 Mbps substream
```

**Done when:** `.mp4` files accumulate, `sourceOnDemand: no` is confirmed in the config.

---

## Step 11 — Phase 4 sign-off

### 11.1 iptables: RTSP confined to VPN

Confirm RTSP is only reachable from `wg0` or `lo`, not the Wi-Fi IP:

```bash
# From a host on the same LAN as the Jetson Wi-Fi (should timeout/refuse)
curl -v --connect-timeout 3 telnet://<JETSON-WIFI-IP>:8554

# From a WireGuard peer (should succeed)
ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1
```

Confirm SSH still works over Wi-Fi WAN after firewall rules are applied:

```bash
ssh khalifa@<JETSON-WIFI-IP>
# must connect — firewall must never block WAN SSH
```

### 11.2 24-hour zero-egress capture

1. Start Wireshark on the laptop connected to **mirror port 1**.
2. Capture filter: `host 192.168.10.108` (add `.109`–`.111` if multiple cameras).
3. Let it run for **24 hours** with the tower powered and cameras streaming.
4. When done, apply display filter:

```
ip.src == 192.168.10.108 && ip.dst != 192.168.10.2
```

Repeat for each camera IP (`.109`, `.110`, `.111`) if present.

**Pass:** zero packets matching this filter.
**Fail:** any packet — Dahua cloud/P2P feature still active; disable it (Step 4) and repeat.

Save the pcap: `pilot_zero_egress_YYYYMMDD.pcap` in `planning/artifacts/`.

### 11.3 PTZ 1,000-command benchmark

```bash
python3 scripts/kallon-ptz-benchmark.py --count 1000
```

| Result | Action |
|--------|--------|
| p95 < 100 ms | Document as PASS |
| p95 ~1.5–2 s | Document as re-baselined (Dahua ONVIF ceiling — acceptable finding) |

Save the table output to `planning/artifacts/ptz-benchmark-YYYYMMDD.txt`.

### 11.4 SMART health probe

```bash
journalctl -u kallon-watchdog | grep -i nvme
# expected: no DISK_FAULT alerts; SMART read at each poll cycle
```

### 11.5 NVMe SMART check

```bash
sudo smartctl -a /dev/nvme0
# expected: critical_warning: 0, media_errors: 0
```

---

## If something goes wrong

| Symptom | Cause | Fix |
|---------|-------|-----|
| Omada **Type** stuck on **Trunk** | Default **Allow All** tag settings on port profile | Per port: **Custom → Native VLAN 10 → Block All**; Type becomes **Access** |
| `ip route get 192.168.10.108` → `enP8p1s0` but ping **Host Unreachable** | L2 split (VLAN mismatch), ACL, camera offline, or wrong IP | Same VLAN 10 Access on ports 2+3; disable ACL to test; `ip neigh` for `FAILED`; camera direct to Jetson; PC on VLAN 10 port ping `.108` |
| `ip neigh` shows `.108 FAILED` | No ARP reply — camera not on same segment or powered | PoE on camera port; confirm camera LED; verify static IP; bypass switch (camera → Jetson direct) |
| `ip route get 192.168.10.108` → `wlP1p1s0` | Camera traffic leaking to Wi-Fi | Use `/24` not `/32` on `CAMERA_JETSON_IP`; re-run modules 30 + 60 |
| SSH drops after install | Default route added to eth | Never set gateway on `CAMERA_IFACE`; re-run module 30 |
| Module 50: `WARN: not a mountpoint` | NVMe not mounted at `RECORD_PATH` | Complete Step 2 before running installer with `RECORD_ENABLE=1` |
| mediamtx crash loop: `unknown field "rtspTransports"` | YAML field renamed in mediamtx ≥1.11; binary is v1.9.3 | Use `protocols: [tcp]` not `rtspTransports`; `git pull` and re-run module 50 |
| mediamtx starts, no recording files | `sourceOnDemand: yes` still in yml | Confirm `RECORD_ENABLE=1` in device.env; re-run module 50; check `sudo cat /etc/mediamtx.yml` |
| Enrollment `401` | Bad enrollment token | Re-run `register-tower` on server; update `ENROLLMENT_TOKEN` |
| Enrollment `409` | Hub not active | Check `list-customers`; re-run fulfillment |
| WG up, no handshake | Peer-add failed | Check enrollment API logs; run `kallon-hub-ssh-verify.ps1` |
| Alerts return 401 | HMAC key mismatch | Re-sync `/etc/kallon/alert.key` tower ↔ hub; restart both services |
| RTSP works locally, fails over VPN from NOC PC | Hub UFW blocks `wg0 → wg0` FORWARD | `kallon-gateway-init.sh` includes `ufw route allow`; existing hubs: `kallon-gateway-ensure-forwarding.sh` |
| RTSP works locally, fails over VPN | iptables or peer missing | Run module 90; `sudo wg show wg0` on both ends |
| Zero-egress capture shows Dahua traffic | P2P/DMSS still active | Return to Step 4; disable cloud features; repeat capture |
| Re-test enrollment | `.enrolled` guard blocking | `sudo rm /etc/kallon/.enrolled` then re-run `kallon-enroll.sh` |

---

## Final checklist

### Control plane
- [ ] Postgres 16 running; `init-schema` done; public port closed
- [ ] `terra-hub-ops.pem` installed; SSH verify passes
- [ ] `cust_lab` active in registry with hub endpoint + pubkey
- [ ] Enrollment API running as service; `healthz` OK from internet
- [ ] Daily `pg_dump` scheduled

### Hardware and network
- [ ] Wi-Fi WAN on `wlP1p1s0`; internet reachable from Jetson
- [ ] NVMe mounted at `/var/kallon/recordings` (separate mountpoint)
- [ ] `smartmontools` installed; `smartctl` reads without error
- [ ] SG2210P: VLAN 10, ACL, mirror configured (port 1 dest, ports 3–6 sources)
- [ ] Camera on **port 3** at `192.168.10.108`; cloud features off; local RTSP works

### Jetson
- [ ] `/etc/kallon/device.env` production values including `RECORD_ENABLE=1`, `ENABLE_NVME=1`
- [ ] `alert.key` identical on tower and hub
- [ ] Full installer modules 00–99 completed without error
- [ ] Enrollment complete (`/etc/kallon/.enrolled` exists)
- [ ] `kallon-acceptance.sh` exit 0; all checks PASS (no WARN on WG)
- [ ] Recording files growing in `/var/kallon/recordings/cam1/`

### Phase 4 sign-off
- [ ] RTSP streams over VPN
- [ ] HMAC alerts return HTTP 200 on hub
- [ ] iptables: RTSP blocked on WAN IP; SSH survives
- [ ] 24 h zero-egress pcap saved; zero camera → third-party traffic
- [ ] PTZ benchmark result documented
- [ ] NVMe SMART clean; `ENABLE_NVME=1` active in watchdog

---

## Next milestones (not in this walkthrough)

| Milestone | Phase | Trigger |
|-----------|-------|---------|
| LTE modem — `WAN_FALLBACK_IFACE=usb0` | 5 | Modem hardware acquired |
| Two-tower test | 4/3 | Second Jetson |
| Real customer order via `kallon-fulfill-order` | 3 | Sales order |
| Longer retention / main-stream recording | — | Adjust `RECORD_MEDIAMTX_DELETE_AFTER` + `RECORD_PATH` partition size |
| Golden image | 7 | Pilot sign-off complete |

---

*Terra Industries · Kallon Sentry Tower · Pilot Hardware Bring-Up Walkthrough · June 2026*
