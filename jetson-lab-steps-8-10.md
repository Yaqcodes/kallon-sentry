# Jetson lab walkthrough — steps 8–10 (no managed switch)

**Scope:** Phase 3 prep (WireGuard + mediamtx), provisioning/watchdog scripts, and what to defer until hardware arrives.  
**Prerequisite:** Jetson on a LAN with internet; camera reachable from Jetson (direct Ethernet or home router).  
**Not required:** PoE managed switch, NVMe SSD, LTE modem, tamper sensors.

The managed switch is only needed later for **Phase 1 proof** (camera VLAN + ACL + mirror capture) and **Phase 3 sign-off** (two customer tunnels on one bench L2 with packet-capture isolation).

---

## Assumptions


| Item                                  | Example                     |
| ------------------------------------- | --------------------------- |
| Home/camera LAN                       | `192.168.1.0/24`            |
| Lab VPN subnet (must not overlap LAN) | `10.50.0.0/24`              |
| Jetson VPN address                    | `10.50.0.2/24`              |
| Customer gateway (lab peer)           | `10.50.0.1/24`, UDP `51820` |
| Future NOC / VLC host                 | `10.50.0.10/24`             |


**Jetson `AllowedIPs` must not be `0.0.0.0/0`.** Scope to the customer/NOC range only (e.g. `10.50.0.0/24`).

**Lab gateway options:** cheap VPS (best production analog), WireGuard on a Windows PC, or a second Linux host on the same LAN.

---

## Step 8 — WireGuard skeleton + mediamtx (Phase 3 lab)

### 8.0 Reset Jetson WireGuard (clean slate)

Run on the **Jetson** only. Does not remove `wireguard-tools` / `wireguard-go` packages.

```bash
sudo systemctl disable --now wg-quick@wg0 2>/dev/null || true
sudo systemctl daemon-reload
sudo wg-quick down wg0 2>/dev/null || true
sudo ip link del wg0 2>/dev/null || true
sudo pkill -f '/usr/bin/wireguard wg0' 2>/dev/null || true

sudo rm -f /etc/wireguard/wg0.conf
sudo rm -f /etc/wireguard/jetson.private /etc/wireguard/jetson.public
sudo rm -f /etc/wireguard/gateway.public
sudo rm -rf /etc/systemd/system/wg-quick@wg0.service.d

# optional: remove packages too
# sudo apt remove -y wireguard-tools wireguard-go
# sudo rm -f /usr/local/bin/wireguard-go
```

For a **full lab reset**, also on the **VPS**: `sudo wg-quick down wg0`, remove `/etc/wireguard/`*, regenerate gateway keys (8.4), open **UDP 51820** in Lightsail, then redo 8.3–8.5 so keys match.

**Camera route without netplan:** if `/etc/netplan/` does not exist on the Jetson, use `kallon-camera-route.service` (see `kallon_mass_deployment_roadmap.md` §6).

### 8.1 Install on Jetson

```bash
sudo apt update
sudo apt install -y wireguard-tools wireguard-go ffmpeg curl
```

**Jetson / L4T note:** Tegra kernels (`*-tegra`) often ship **without** the in-kernel WireGuard module even on 5.10+. `wireguard-go` lets `wg-quick` use a userspace implementation (fine for lab). If `wg-quick up wg0` fails with `Unknown device type` / `Protocol not supported`, confirm `wireguard-go` is installed and retry.

Clone the stack:

```bash
git clone https://github.com/Yaqcodes/kallon-sentry.git /opt/kallon
cd /opt/kallon
pip3 install -r requirements.txt
```

Install **mediamtx** (check [releases](https://github.com/bluenviron/mediamtx/releases) for current version):

```bash
ARCH=arm64
VER=v1.11.3
curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/${VER}/mediamtx_${VER}_linux_${ARCH}.tar.gz" \
  | sudo tar -xz -C /usr/local/bin mediamtx
sudo useradd -r -s /usr/sbin/nologin mediamtx 2>/dev/null || true
```

### 8.2 Lab addressing


| Role                      | WireGuard IP    | Notes               |
| ------------------------- | --------------- | ------------------- |
| Customer gateway (peer)   | `10.50.0.1/24`  | Listens UDP `51820` |
| Jetson (this device)      | `10.50.0.2/24`  | Interface `wg0`     |
| NOC / VLC host (optional) | `10.50.0.10/24` | On gateway or VPN   |


### 8.3 Keys on Jetson

```bash
sudo install -d -m 700 /etc/wireguard
wg genkey | sudo tee /etc/wireguard/jetson.private | wg pubkey | sudo tee /etc/wireguard/jetson.public
sudo chmod 600 /etc/wireguard/jetson.private
```

Save **jetson.public** for the gateway peer block.

### 8.4 Customer gateway (lab peer)

#### Option A — VPS (recommended)

Ubuntu VPS with public IP; firewall allows **UDP 51820**.

```bash
sudo apt update
sudo apt install -y wireguard-tools
```

Use `**wireguard-tools**` (same as step 8.1 on the Jetson). The meta package `wireguard` is missing on many Ubuntu/Debian images; the kernel module is already built in on 5.6+.

If `wg` is missing after install, enable **universe** then retry: `sudo add-apt-repository universe && sudo apt update`.

**Not on Linux?** For a Windows lab gateway, install [WireGuard for Windows](https://www.wireguard.com/install/) and create the tunnel in the GUI (no `apt`). Use the same keys/`wg0.conf` peer settings below.

```bash
wg genkey | sudo tee /etc/wireguard/gateway.private | wg pubkey | sudo tee /etc/wireguard/gateway.public
sudo chmod 600 /etc/wireguard/gateway.private
```

`/etc/wireguard/wg0.conf` on the **gateway**:

```ini
[Interface]
Address = 10.50.0.1/24
ListenPort = 51820
PrivateKey = <gateway.private key>

[Peer]
# Jetson unit 001
PublicKey = <jetson.public key>
AllowedIPs = 10.50.0.2/32
PersistentKeepalive = 25
```

```bash
sudo systemctl enable wg-quick@wg0
sudo wg-quick up wg0
```

#### Option B — Same LAN

Use the gateway machine’s **LAN IP** as `Endpoint` on the Jetson (port `51820`). No port forward required.

### 8.5 Jetson `/etc/wireguard/wg0.conf`

```ini
[Interface]
Address = 10.50.0.2/24
PrivateKey = <jetson.private key>

[Peer]
PublicKey = <gateway.public key>
Endpoint = <gateway.public.ip.or.lan>:51820
AllowedIPs = 10.50.0.0/24
PersistentKeepalive = 25
```

Enable and test (Jetson **must** use userspace on tegra — see 8.0 / troubleshooting):

```bash
sudo mkdir -p /etc/systemd/system/wg-quick@wg0.service.d
printf '%s\n' '[Service]' 'Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/bin/wireguard' \
  | sudo tee /etc/systemd/system/wg-quick@wg0.service.d/userspace.conf
sudo systemctl daemon-reload
```

**Do not** run manual `wg-quick up` and `systemctl start` on a live `wg0` — you get `wg0 already exists`. Tear down first, then enable systemd only:

```bash
sudo wg-quick down wg0 2>/dev/null || true
sudo ip link del wg0 2>/dev/null || true
sudo systemctl reset-failed wg-quick@wg0 2>/dev/null || true
sudo systemctl enable --now wg-quick@wg0
sudo systemctl status wg-quick@wg0 --no-pager
sudo wg show
ping -c 3 10.50.0.1
```

If the service still fails: `journalctl -xeu wg-quick@wg0.service --no-pager | tail -30`

`ip -d link show wg0` should show `tun type tun` (userspace), not kernel `wireguard`.

If **latest handshake** stays empty: wrong keys, UDP blocked, or bad `Endpoint`. Fix before mediamtx.

**Jetson `wg-quick` fails with `Unknown device type` / `Protocol not supported`:**

Tegra kernels often have no working in-kernel WireGuard. Install userspace tools, then diagnose:

```bash
uname -r
sudo modprobe wireguard 2>&1 || true
dpkg -L wireguard-go | grep /usr/bin
# Jammy/Jetson: binary is often /usr/bin/wireguard (not wireguard-go) — see below
grep -F 'wireguard-go' "$(command -v wg-quick)" || echo "wg-quick has NO wireguard-go fallback — use manual up below"
ls -l /dev/net/tun
```

1. **Force userspace** (needs recent `wireguard-tools` with fallback in `wg-quick`):

```bash
sudo apt install -y wireguard-tools wireguard-go
sudo wg-quick down wg0 2>/dev/null || true
sudo WG_QUICK_USERSPACE_IMPLEMENTATION=wireguard-go wg-quick up wg0
```

On **Ubuntu 22.04 / Jammy (typical Jetson)**, the `wireguard-go` package installs the userspace binary as `**/usr/bin/wireguard`**, not `wireguard-go`, so `which wireguard-go` is empty and `wg-quick` will not fall back unless you override:

```bash
sudo WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/bin/wireguard wg-quick up wg0
# or: sudo ln -sf /usr/bin/wireguard /usr/local/bin/wireguard-go
```

You should see: `[!] Missing WireGuard kernel module. Falling back to slow userspace implementation.`

1. **If there is still no fallback line** — old `wg-quick`; bring the tunnel up manually:

```bash
sudo wg-quick down wg0 2>/dev/null || true
sudo ip link del wg0 2>/dev/null || true
sudo pkill -f 'wireguard-go wg0' 2>/dev/null || true

sudo /usr/bin/wireguard wg0
sudo wg addconf wg0 <(sudo wg-quick strip wg0)
sudo ip -4 address add 10.50.0.2/24 dev wg0
sudo ip link set up dev wg0
sudo wg show
ping -c 3 10.50.0.1
```

Adjust the `ip address add` line to match `Address` in your `wg0.conf`.

1. **Persist for systemd** (after manual up works):

```bash
sudo mkdir -p /etc/systemd/system/wg-quick@wg0.service.d
printf '%s\n' '[Service]' 'Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/bin/wireguard' \
  | sudo tee /etc/systemd/system/wg-quick@wg0.service.d/userspace.conf
sudo systemctl daemon-reload
```

If `wireguard-go` errors on TUN: `sudo modprobe tun` and ensure `/dev/net/tun` exists.

For production you may later build the kernel module (`wireguard-dkms` or [wireguard-linux-compat](https://forums.developer.nvidia.com/t/jetson-agx-orin-where-is-wireguard/332284) on tegra); lab VPN only needs a working tunnel.

### 8.6 mediamtx — local camera in, VPN out

Confirm local RTSP URL:

```bash
export CAMERA_PASSWORD='your_password'
python3 /opt/kallon/dahua_onvif_control.py rtsp
```

Create `/etc/mediamtx.yml`:

```yaml
rtspAddress: :8554
paths:
  cam1:
    source: rtsp://admin:PASSWORD@192.168.1.108/cam/realmonitor?channel=1&subtype=0
    sourceOnDemand: yes
    rtspTransport: tcp
```

Create `/etc/systemd/system/mediamtx.service`:

```ini
[Unit]
Description=mediamtx RTSP server
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx.yml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
```

On the **gateway or NOC machine** (with route to `10.50.0.2` over VPN):

```bash
ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1
```

Or VLC → Open Network Stream → same URL.

**Path without switch:** camera → Jetson (LAN) → mediamtx → `wg0` → gateway → VLC. Validates Phase 3 video over VPN; does **not** prove camera VLAN isolation (Phase 1).

### 8.7 Partial security check (until switch arrives)

Host firewall on Jetson — RTSP only from `wg0`:

```bash
sudo iptables -A INPUT -i wg0 -p tcp --dport 8554 -j ACCEP
```

Persist with `iptables-persistent` or netfilter rules in your image. Full “off-VPN cannot see stream” sign-off needs the managed switch + ACL design from the sovereign stack brief.

---

## Step 9 — Scripts to build now (no switch, no GPIO)

### 9.1 Provisioning layout

```bash
sudo install -d -m 700 /etc/kallon
```


| Path                                  | Purpose                                                                               |
| ------------------------------------- | ------------------------------------------------------------------------------------- |
| `/etc/kallon/device.env`              | `DEVICE_ID`, `WG_ENDPOINT`, `WG_PEER_PUBKEY`, `WG_ADDRESS`, `WG_ALLOWED` (mode `600`) |
| `/etc/wireguard/jetson.private`       | Generated once; never in git                                                          |
| `/etc/wireguard/wg0.conf`             | Rendered from template                                                                |
| `/usr/local/sbin/kallon-wg-provision` | Keygen + template render + `systemctl enable wg-quick@wg0`                            |


**Provisioning flow:**

1. Generate `jetson.private` / `jetson.public` if missing.
2. Read `/etc/kallon/device.env`.
3. Write `/etc/wireguard/wg0.conf`.
4. Enable `wg-quick@wg0`.

### 9.2 WireGuard reconnect watchdog

Brief requirement: restart if no handshake in **60 seconds**. Use a **systemd timer** (not a blocking loop).

`/usr/local/sbin/kallon-wg-watchdog.sh`:

```bash
#!/bin/bash
set -euo pipefail
HS=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}')
NOW=$(date +%s)
if [ -z "$HS" ] || [ "$HS" = "0" ] || [ $((NOW - HS)) -gt 60 ]; then
  systemctl restart wg-quick@wg0
fi
```

`/etc/systemd/system/kallon-wg-watchdog.service`:

```ini
[Unit]
Description=Restart WireGuard if handshake stale

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/kallon-wg-watchdog.sh
```

`/etc/systemd/system/kallon-wg-watchdog.timer`:

```ini
[Unit]
Description=Check WireGuard handshake every 30s

[Timer]
OnBootSec=30
OnUnitActiveSec=30

[Install]
WantedBy=timers.target
```

```bash
sudo chmod +x /usr/local/sbin/kallon-wg-watchdog.sh
sudo systemctl enable --now kallon-wg-watchdog.timer
```

**Test:** stop `wg-quick@wg0` on the gateway; within ~60s Jetson should restart `wg0` and handshake should return.

### 9.3 Health watchdog skeleton (Phase 4 prep)

Implement first:


| Check                                   | Interval | Alert type           |
| --------------------------------------- | -------- | -------------------- |
| `ffprobe` on local RTSP URL, 5s timeout | 10s      | `CAMERA_STREAM_FAIL` |
| `thermal_zone` > 80°C                   | 10s      | `TEMP_CRITICAL`      |


Defer until hardware: `smartctl` (NVMe), GPIO reed/light, MPU-6050.

**Alert payload** (from sovereign stack brief):

```json
{
  "device_id": "kallon-unit-001",
  "timestamp_utc": "2026-05-14T10:23:44Z",
  "alert_type": "CAMERA_STREAM_FAIL",
  "severity": "HIGH",
  "details": {},
  "hmac": "<sha256-hmac-hex>"
}
```

Shared secret: `/etc/kallon/alert.key` (mode `600`).  
Delivery: `HTTP POST` to `ALERT_WEBHOOK_URL` over VPN (e.g. `http://10.50.0.1:8080/alerts`).

HMAC example:

```bash
BODY='{"device_id":"kallon-001","timestamp_utc":"2026-05-14T12:00:00Z","alert_type":"CAMERA_STREAM_FAIL","severity":"HIGH","details":{}}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$(cat /etc/kallon/alert.key)" | awk '{print $2}')
curl -sS -X POST "$ALERT_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Kallon-Signature: $SIG" \
  -d "$BODY"
```

Run as `kallon-watchdog.service` with `Restart=on-failure` (same pattern as `kallon-ptz-daemon`).

**Lab webhook:** minimal listener on the gateway that logs POST bodies; replace with customer NOC in production.

### 9.4 Git vs device secrets


| In repo (`kallon-sentry`)            | On device only                                                                |
| ------------------------------------ | ----------------------------------------------------------------------------- |
| `deploy/*.example`, script templates | `/etc/wireguard/*.private`, `/etc/kallon/device.env`, `/etc/kallon/alert.key` |
| `HOW_TO_USE.md`, this guide          | `/etc/systemd/system/*.service` (installed from examples)                     |


---

## Step 10 — Defer until hardware / switch


| Item                                       | Blocked by                 | Do now instead                                                      |
| ------------------------------------------ | -------------------------- | ------------------------------------------------------------------- |
| Camera VLAN + ACL (cameras → Jetson only)  | Managed PoE switch         | Direct Jetson↔camera cable; disable P2P/DMSS in camera UI           |
| 24h Wireshark soak, zero Dahua egress      | Switch mirror port         | Short capture on Jetson NIC; label **inconclusive**                 |
| Two-tunnel cross-customer isolation (pcap) | Switch + second WG profile | Optional second subnet (`10.51.0.0/24`) on gateway; full test later |
| Multi-camera / PoE bench                   | Switch                     | Single camera                                                       |
| LTE WAN + WG under NAT                     | Cellular modem             | Home WAN; keep `PersistentKeepalive = 25`                           |
| NVMe SMART / disk alerts                   | SSD                        | Omit `smartctl`                                                     |
| Door reed, light, accelerometer            | GPIO/I2C wiring            | RTSP + temperature only                                             |
| Phase 1 exit sign-off                      | Switch + stable camera     | Camera UI + local RTSP                                              |
| Phase 3 exit sign-off                      | Switch + isolation proof   | Lab WG + mediamtx + iptables partial check                          |


---

## Recommended order (no switch)

1. `git clone` → `python3 dahua_onvif_control.py test`
2. WireGuard up → `ping 10.50.0.1`
3. mediamtx → `ffprobe` from gateway over VPN
4. Enable `kallon-wg-watchdog.timer`
5. Health watchdog: RTSP fail → signed POST over tunnel
6. PTZ daemon (`deploy/kallon-ptz-daemon.service.example`) — parallel track

---

## Quick reference

```bash
git clone https://github.com/Yaqcodes/kallon-sentry.git /opt/kallon
cd /opt/kallon
pip3 install -r requirements.txt
export CAMERA_PASSWORD='...'

sudo wg-quick up wg0 && sudo wg show
ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1   # from NOC, over VPN
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1 # on Jetson, local
```

---

## Related docs

- `kallon_sovereign_stack_brief.md` — full phased plan and exit criteria
- `kallon_mass_deployment_roadmap.md` — lab → factory → customer deploy checklist
- `HOW_TO_USE.md` — ONVIF / PTZ daemon usage
- `deploy/kallon-ptz-daemon.service.example` — PTZ systemd unit

---

*Terra Industries · Kallon Sentry Tower · Lab guide v1.0*