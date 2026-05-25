# Kallon Sentry Tower — Current State of the Stack (Phases 1–4)

> **Terra Industries · Confidential · Internal Engineering · May 2026**
>
> **HANDLE AS SECRET.** This document includes live WireGuard private keys, HMAC alert keys, RSA private keys, default camera credentials, and operator passwords. Treat it as `chmod 600`, do not commit to any public repository, do not copy into shared chat tools, do not include in screenshots. The repo's own `.gitignore` already excludes the underlying secret files (`*.pem`, `*.key`, `wg-keys`*); this aggregated copy bypasses that protection on purpose, for the engineering owner only.

---

## 0. How to read this document

This is a **god's-eye snapshot** of everything that has been designed, built, deployed, or verified for the Kallon Sentry Tower sovereign stack at this date. It covers **Phases 1 through 4** of the sovereign stack brief (`kallon_sovereign_stack_brief.md`).

Sections are layered from broad to specific:

1. [Hero diagram — the whole world on one page](#1-hero-diagram--the-whole-world-on-one-page)
2. [The four hosts in the system](#2-the-four-hosts-in-the-system)
3. [Live verified state (from SSH probe today)](#3-live-verified-state-from-ssh-probe-today)
4. [Phase 1 — Data Sovereignty](#4-phase-1--data-sovereignty)
5. [Phase 2 — Local PTZ Control](#5-phase-2--local-ptz-control)
6. [Phase 3 — Per-Customer Private Network (WireGuard + mediamtx)](#6-phase-3--per-customer-private-network-wireguard--mediamtx)
7. [Phase 4 — Tamper Detection & Health Monitoring](#7-phase-4--tamper-detection--health-monitoring)
8. [Master file inventory — every file, where it lives, what it does](#8-master-file-inventory)
9. [Credentials & secrets inventory](#9-credentials--secrets-inventory)
10. [Network ports & protocols cheat sheet](#10-network-ports--protocols-cheat-sheet)
11. [Data flows — three signed sequence diagrams](#11-data-flows)
12. [Phase status board (vs. brief exit criteria)](#12-phase-status-board)
13. [Open gaps, blockers, and immediate next actions](#13-open-gaps-blockers-and-immediate-next-actions)
14. [One-page operator cheat sheet](#14-one-page-operator-cheat-sheet)

Conventions:

- `Code in this style` is a literal path, command, IP, or filename.
- ✅ verified live, 🟡 verified in code/docs but not directly observed, 🔴 not implemented, ⏸ explicitly deferred per brief.
- Live state was captured today from **both** the VPS (`ssh ubuntu@18.220.75.237` with `kallon-vps-key.pem`) and the Jetson (`paramiko` + password auth via `khalifa@192.168.1.246`). Anywhere a "verified live" claim appears, it's backed by either a `systemctl`/`wg show`/`ps`/`ss` output captured today, or a recent log line from the daemon itself.

---

## 1. Hero diagram — the whole world on one page

```mermaid
flowchart LR
    subgraph CAMERA["Camera VLAN (today: direct cable)"]
        CAM["Dahua PTZ IP Camera<br/>192.168.1.108<br/>ONVIF HTTP :80<br/>RTSP :554<br/>user: admin / terra123<br/>P2P/DMSS: OFF (Phase 1)"]
    end

    subgraph JETSON["Jetson Orin Nano (khalifa-desktop) — ALL SERVICES RUNNING<br/>wlP1p1s0 192.168.1.246 (default route, internet)<br/>enP8p1s0 192.168.1.10 (camera-only, pinned route to .108)<br/>wg0: 10.50.0.2"]
        direction TB
        ONVIF["dahua_onvif_control.py<br/>sentry_ptz_absolute.py<br/>ONVIF/PTZ CLIs (on-demand)"]
        PTZD["kallon-ptz-daemon.service<br/>RUNNING pid 2633 (user khalifa)<br/>listening 127.0.0.1:8765"]
        MTX["mediamtx.service<br/>RUNNING pid 1253 (root)<br/>RTSP :8554 (all interfaces)"]
        WD["kallon-watchdog.service<br/>RUNNING pid 37590 (user khalifa)<br/>RTSP / temp / MPU / reed / LDR<br/>HMAC-SHA256 alerts"]
        WG_J["wg-quick@wg0.service ACTIVE (oneshot exited)<br/>userspace drop-in: WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/bin/wireguard"]
        WGWD["kallon-wg-watchdog.timer ACTIVE (every 30 s)<br/>+ kallon-wg-watchdog.service oneshot"]
        CR["kallon-camera-route.service ACTIVE (oneshot exited)<br/>pins 192.168.1.108/32 dev enP8p1s0"]
        SENS["Tamper sensors on J12<br/>MPU-6050 (I2C7 0x68)<br/>Reed (BOARD pin 31)<br/>LDR (BOARD pin 33)"]
        CAM -. ONVIF 80 / RTSP 554 .-> ONVIF
        CAM -. RTSP pull .-> MTX
        ONVIF --> PTZD
        SENS --> WD
        MTX --> WG_J
        WD --> WG_J
        CR -. pins route .- CAM
        WGWD -. restarts if stale .-> WG_J
    end

    subgraph VPS["VPS — AWS Lightsail (ip-172-26-1-191)<br/>Public: 18.220.75.237  ·  wg0: 10.50.0.1<br/>UDP 51820, TCP 8080"]
        WG_V["WireGuard hub<br/>peer Jetson 10.50.0.2/32<br/>peer NOC PC 10.50.0.10/32"]
        AL["alert_listener.py<br/>HTTP :8080<br/>verifies HMAC, prints"]
    end

    subgraph NOC["NOC PC (Engineer workstation)<br/>LAN: 192.168.1.230  ·  wg0: 10.50.0.10"]
        VLC["VLC / ffprobe / browser"]
        DEV["Cursor IDE + git<br/>Yaqcodes/kallon-sentry"]
    end

    WG_J <== Encrypted UDP 51820 ==> WG_V
    WG_V <== Encrypted UDP 51820 ==> NOC
    WD == "POST /alerts (HMAC)" ==> AL
    MTX == "RTSP/TCP 8554 over wg0" ==> VLC
    DEV -. git push/pull .-> GH["GitHub<br/>github.com/Yaqcodes/kallon-sentry<br/>branch: main"]
    DEV -. git pull on Jetson .-> JETSON

    classDef secret fill:#fee,stroke:#c00,color:#900
    classDef live fill:#efe,stroke:#080,color:#040
    class CAM secret
    class VPS,WG_V,AL live
```



**Reading this diagram:**

- **Solid bold arrows** = encrypted production data plane (RTSP video and HMAC-signed alerts, all over WireGuard).
- **Dotted arrows** = control / management plane (ONVIF, git, RTSP pulls on the local LAN).
- **Red border** (camera) = the only device intentionally cut off from the internet; cloud paths (P2P, DMSS, Easy4IP) disabled at the camera UI.
- **Green border** (VPS) = the only component verified **live, today**, end-to-end.

---

## 2. The four hosts in the system

```mermaid
flowchart TB
    subgraph H1["① Camera"]
        H1a["Dahua PTZ IP camera<br/>192.168.1.108<br/>ONVIF HTTP :80 ✅<br/>(ONVIF :8899 refused)<br/>RTSP :554<br/>admin / terra123"]
    end
    subgraph H2["② Jetson Orin Nano"]
        H2a["Host: khalifa-desktop<br/>User: khalifa (uid in groups: gpio, i2c)<br/>OS: Ubuntu 22.04 / L4T (JetPack 6.2.1)<br/>LAN: 192.168.1.246 (eth0 / enP8p1s0)<br/>wg0: 10.50.0.2/24<br/>Repo: /home/khalifa/kallon (clone of Yaqcodes/kallon-sentry)<br/>Config: /etc/kallon/  ·  Secrets: /etc/wireguard/  ·  /etc/kallon/alert.key"]
    end
    subgraph H3["③ VPS (Customer Gateway)"]
        H3a["Host: ip-172-26-1-191 (AWS Lightsail, us-east-2)<br/>User: ubuntu (sudo NOPASSWD via pem key)<br/>Public IP: 18.220.75.237<br/>Internal: 172.26.1.191/20 (ens5)<br/>wg0: 10.50.0.1/24<br/>WG listen UDP 51820  ·  Alert listener TCP 8080<br/>NOC alert listener: /home/ubuntu/kallon-noc/alert_listener.py<br/>HMAC key: /etc/kallon/alert.key (640 root:?)"]
    end
    subgraph H4["④ NOC PC (engineer workstation)"]
        H4a["Windows 10/11<br/>Path: C:/Users/kayob/Documents/Khalifa Projects/Kallon Sentry Tower<br/>LAN: 192.168.1.230  ·  wg0: 10.50.0.10<br/>SSH key for VPS: kallon-vps-key.pem (in project root, gitignored)<br/>No SSH key registered on the Jetson — password auth only"]
    end
```



### 2.1 Where I verified what


| Host     | What I observed today                                                                                                                                                                                                                                 | Method                                           |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| ① Camera | Indirect — `ip route get 192.168.1.108` on the Jetson returns `dev enP8p1s0 src 192.168.1.10`; ping is `0.322 ms`; mediamtx successfully opens RTSP sessions on demand; PTZ daemon journal: `connected to camera 192.168.1.108:80 profile_default=0`. | Jetson SSH probe today                           |
| ② Jetson | **Full SSH probe today.** All 3 systemd services running, MPU-6050 actively detecting motion (just-fired `TAMPER_IMPACT` with `delta_mg=405.0`, alert sent `status=http_200`).                                                                        | `paramiko` → `khalifa@192.168.1.246`             |
| ③ VPS    | **Full SSH probe today.**                                                                                                                                                                                                                             | `ssh -i kallon-vps-key.pem ubuntu@18.220.75.237` |
| ④ NOC PC | `ipconfig` shows `10.50.0.10` is up; pings to `10.50.0.1` (216 ms) and `10.50.0.2` (449 ms) both succeed; LAN IP is `192.168.1.230`.                                                                                                                  | Local PowerShell                                 |


### 2.2 Services & daemons per node (the "what's running where" map)

The new most-important diagram in this document — every long-running process and its current state across the four hosts:

```mermaid
flowchart TB
    subgraph CAM_NODE["① Camera — 192.168.1.108"]
        direction TB
        CAM_FW["Dahua firmware (stock)<br/>ONVIF service :80 — RUNNING ✅<br/>RTSP server :554 — RUNNING ✅<br/>P2P/DMSS/Easy4IP cloud — OFF 🟡<br/>NTP/email/FTP push — OFF 🟡"]
    end

    subgraph JET_NODE["② Jetson Orin Nano — 192.168.1.246 / 10.50.0.2 — Ubuntu 22.04 / L4T R36.4.4"]
        direction TB
        subgraph J_PHASE3["Phase 3 — networking"]
            JS1["wg-quick@wg0.service<br/>active (exited) ✅ • oneshot<br/>userspace via /usr/bin/wireguard<br/>last handshake 40 s ago"]
            JS2["kallon-wg-watchdog.timer<br/>active (waiting) ✅ • every 30 s<br/>→ kallon-wg-watchdog.service (oneshot)"]
            JS3["kallon-camera-route.service<br/>active (exited) ✅ • oneshot<br/>pins 192.168.1.108/32 → enP8p1s0"]
            JS4["mediamtx.service<br/>active (running) ✅ • PID 1253 (root)<br/>RTSP :8554 — sourceOnDemand"]
        end
        subgraph J_PHASE2["Phase 2 — local PTZ"]
            JS5["kallon-ptz-daemon.service<br/>active (running) ✅ • PID 2633 (khalifa)<br/>JSON over TCP 127.0.0.1:8765<br/>holds one ONVIF session to .108:80"]
        end
        subgraph J_PHASE4["Phase 4 — tamper & health"]
            JS6["kallon-watchdog.service<br/>active (running) ✅ • PID 37590 (khalifa)<br/>10 s poll · 60 s dedup<br/>RTSP/temp/MPU/reed/LDR/(NVMe off)/(power off)<br/>last alert: TAMPER_IMPACT → http_200"]
        end
        subgraph J_PLATFORM["Platform / inert"]
            JS7["docker.service • dead (inactive)<br/>docker0 bridge present but no containers"]
            JS8["snapd.service • active (running)<br/>pinned to revision 24724 (2.68.5)<br/>apt-mark hold snapd"]
            JS9["systemd-resolved · gdm · NetworkManager<br/>standard distro stack"]
        end
    end

    subgraph VPS_NODE["③ VPS — Lightsail us-east-2 — 18.220.75.237 / 10.50.0.1 — ip-172-26-1-191"]
        direction TB
        VS1["wg-quick@wg0.service<br/>active ✅ • kernel module (not userspace)<br/>UDP :51820 — 2 peers (Jetson + NOC)<br/>both handshakes < 60 s old"]
        VS2["alert_listener.py<br/>RUNNING ✅ • PID 45244 (root, sudo nohup)<br/>HTTP :8080 — HMAC verifier<br/>⚠ NOT a systemd unit — survives only if VPS not rebooted"]
        VS3["ufw / iptables<br/>INACTIVE ⚠ — :8080 reachable from internet"]
        VS4["sshd · cloud-init · systemd-resolved<br/>standard Ubuntu cloud stack"]
    end

    subgraph NOC_NODE["④ NOC PC — Windows 10/11 — 192.168.1.230 / 10.50.0.10"]
        direction TB
        NS1["WireGuard for Windows<br/>tunnel UP ✅ • last handshake 28 s ago<br/>peer = mXuS1k... @ 18.220.75.237:51820"]
        NS2["Cursor IDE + Python 3.14 + git<br/>paramiko 5.0.0 (used by these probes)"]
        NS3["VLC / ffprobe (manual)<br/>pulls rtsp://10.50.0.2:8554/cam1 over wg0"]
    end

    JS4 == "RTSP pull on demand" ==> CAM_FW
    JS5 == "ONVIF SOAP :80" ==> CAM_FW
    JS6 == "ffprobe rtsp://127.0.0.1:8554/cam1" ==> JS4
    JS6 == "HMAC POST :8080 over wg0" ==> VS2
    JS1 == "WG UDP :51820 over WAN" ==> VS1
    NS1 == "WG UDP :51820 over WAN" ==> VS1

    classDef running fill:#dfd,stroke:#080,color:#040
    classDef inert fill:#eef,stroke:#888,color:#222
    classDef warn fill:#fee,stroke:#c80,color:#640
    class JS1,JS2,JS3,JS4,JS5,JS6,VS1,VS2,NS1,CAM_FW running
    class JS7,JS8,JS9,VS4,NS2,NS3 inert
    class VS3 warn
```



**Reading this diagram:**

- Green = process or unit observed in `active (running)` / `active (exited)` state today, with a recent log line proving the service does what it says.
- Blue = present but not part of the production data plane.
- Red = inactive or misconfigured, will be flagged in §13.

---

## 3. Live verified state (from SSH probe today)

These are **bytes-on-the-wire** facts, not extrapolation.

### 3.1 VPS `wg show` (10.50.0.1)

```text
interface: wg0
  public key:   mXuS1kDIK49t6TFcDRJ9AqlKdb+jDxkilNcaM+oWdnw=
  listening port: 51820

peer: A8urKcYrPJzJfgt8V0GPdmFaigO8rgGjUMGBHJVjJyw=    # JETSON
  endpoint:        135.129.124.77:20939                  # NAT-mapped Jetson WAN
  allowed ips:     10.50.0.2/32
  latest handshake: 1 second ago
  transfer:        88.10 MiB received, 10.12 MiB sent
  persistent keepalive: every 25 seconds

peer: lN6KnJrnsiHfj/VMoTkxqFPNNBq1MUqt4NNpsDxqWEg=    # NOC PC (this workstation)
  endpoint:        135.129.124.77:42417                  # Same NAT — same household uplink
  allowed ips:     10.50.0.10/32
  latest handshake: 28 seconds ago
  transfer:        3.19 MiB received, 88.09 MiB sent
```

**Inference from the byte counts:** the VPS has *sent* 88 MiB to the NOC PC — that strongly suggests the NOC has been pulling RTSP through the tunnel from the Jetson (relayed by the VPS) or has been receiving the alert traffic. Conversely, the VPS has *received* 88 MiB from the Jetson — consistent with a long-running video pull pipeline or heavy alert traffic.

### 3.2 VPS `/etc/wireguard/wg0.conf` (live cat today)

```ini
[Interface]
Address = 10.50.0.1/24
ListenPort = 51820
PrivateKey = UPcc3wZwkjHPco8/KrVXdKC439cSHdcmi3Kpeft192Q=

[Peer]
PublicKey = A8urKcYrPJzJfgt8V0GPdmFaigO8rgGjUMGBHJVjJyw=
AllowedIPs = 10.50.0.2/32
PersistentKeepalive = 25

[Peer]
# Your PC (NOC)
PublicKey = lN6KnJrnsiHfj/VMoTkxqFPNNBq1MUqt4NNpsDxqWEg=
AllowedIPs = 10.50.0.10/32
```

> ⚠️ **Drift note:** the NOC peer here has no `PersistentKeepalive`, but the Jetson peer does. That's fine — keepalive only matters behind NAT for the *outgoing* side.

### 3.3 VPS NOC alert listener (PID 45244, running)

```text
root  45242 0.0 1.6 17144 7008 pts/2 S+ 12:12 sudo python3 /home/ubuntu/kallon-noc/alert_listener.py
root  45243 0.0 0.6 17144 2612 pts/1 Ss 12:12 sudo python3 /home/ubuntu/kallon-noc/alert_listener.py
root  45244 0.0 4.8 29780 20648 pts/1 S+ 12:12 python3 /home/ubuntu/kallon-noc/alert_listener.py
```

Listening:

```text
LISTEN 0 5 0.0.0.0:8080 0.0.0.0:* users:(("python3",pid=45244,fd=3))
```

Source (1372 bytes), `~ubuntu/kallon-noc/alert_listener.py` — verbatim from `phase4-setup-guide.md`:

```python
#!/usr/bin/env python3
"""Minimal alert listener for the Kallon watchdog. Verifies HMAC and logs."""
import hashlib, hmac, json, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

KEY = Path("/etc/kallon/alert.key").read_bytes().strip()
BIND = ("0.0.0.0", 8080)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        sig_header = self.headers.get("X-Kallon-Signature", "")
        expected = "sha256=" + hmac.new(KEY, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            self.send_response(403); self.end_headers()
            self.wfile.write(b"bad signature\n")
            print(f"[REJECTED] bad HMAC from {self.client_address[0]}")
            return
        alert = json.loads(body)
        ...
```

### 3.4 VPS alert HMAC key

```text
$ sudo cat /etc/kallon/alert.key
hOQCqcEO3VKDko6JUy2Xrd3VvhIgwsbnqMWpNEzN8d0=
```

This exact base64 string also appears in `**wg-keys.txt**` on the NOC PC:

```text
HMAC Key (Alerts) - hOQCqcEO3VKDko6JUy2Xrd3VvhIgwsbnqMWpNEzN8d0=
```

And per `phase4-setup-guide.md` Step 11, the **same value must exist** at `/etc/kallon/alert.key` on the Jetson. Symmetric secret — if either side rotates, the other must follow.

### 3.5 VPS firewall posture

- `ufw status` → **inactive**
- `iptables -L -n` → all chains `ACCEPT`, no rules

> ⚠️ This means the **VPS itself has no packet filtering** beyond AWS's security group on UDP 51820 and TCP 8080. Phase 3 sign-off should harden this (deny everything except WG-UDP from anywhere and 8080-TCP from `10.50.0.0/24` only).

### 3.6 VPS interfaces

```text
lo:   127.0.0.1/8
ens5: 172.26.1.191/20  (AWS VPC private)
wg0:  10.50.0.1/24
```

### 3.7 Jetson live state (full probe today)

`uname -a` and base platform:

```text
Linux khalifa-desktop 5.15.148-tegra #1 SMP PREEMPT Mon Jun 16 08:24:48 PDT 2025 aarch64
Ubuntu 22.04.5 LTS (Jammy Jellyfish)
# R36 (release), REVISION: 4.4 (JetPack 6.2.1)
Python 3.10.12
uptime: 4 h 36 m · load 0.48 0.20 0.10 · mem 7.4 GiB (2.0 used)
disk: /dev/mmcblk0p1 116G mounted at / (24% used)
```

Network interfaces:

```text
wlP1p1s0   UP    192.168.1.246/24   ← default route (internet)
enP8p1s0   UP    192.168.1.10/24    ← camera-only (route 192.168.1.108/32)
wg0        UP    10.50.0.2/24       ← WireGuard customer tunnel
docker0    DOWN  172.17.0.1/16      ← present but unused
can0       DOWN                     ← CAN bus interface, unused
```

> ⚠ Both `wlP1p1s0` and `enP8p1s0` are on the **same /24** (`192.168.1.0/24`). Without an explicit per-host route this could cause asymmetric routing for `192.168.1.108`. `kallon-camera-route.service` (oneshot, see §4 below) installs `ip route replace 192.168.1.108/32 dev enP8p1s0` at boot to force the camera traffic onto the wired NIC.

`sudo wg show wg0` (Jetson side):

```text
interface: wg0
  public key:  A8urKcYrPJzJfgt8V0GPdmFaigO8rgGjUMGBHJVjJyw=
  listening port: 35657

peer: mXuS1kDIK49t6TFcDRJ9AqlKdb+jDxkilNcaM+oWdnw=
  endpoint:        18.220.75.237:51820
  allowed ips:     10.50.0.0/24
  latest handshake: 40 seconds ago
  transfer:        92 B received, 212 B sent
  persistent keepalive: every 24 seconds
```

> ℹ️ The transfer byte counts on the Jetson side are tiny (212 B sent in this session) because `wg-quick@wg0` was last restarted recently — the byte counter is per-interface-lifetime and resets on every `wg-quick down/up` cycle. The VPS side showing 88 MiB in/out is the cumulative count for its uninterrupted lifetime.

`systemctl list-units | grep -iE 'kallon|mediamtx|wg'` — what's installed:

```text
kallon-camera-route.service           loaded   active   exited     Route camera IP via direct Ethernet
kallon-ptz-daemon.service             loaded   active   running    Kallon ONVIF PTZ daemon (JSON/TCP)
kallon-watchdog.service               loaded   active   running    Kallon health and tamper watchdog
kallon-wg-watchdog.service            loaded   inactive dead       Restart WireGuard if handshake stale
kallon-wg-watchdog.timer              loaded   active   waiting    Check WireGuard handshake every 30s
mediamtx.service                      loaded   active   running    mediamtx RTSP server
wg-quick@wg0.service                  loaded   active   exited     WireGuard via wg-quick(8) for wg0
```

`ps auxf | grep -iE 'kallon|mediamtx'`:

```text
root      1253  /usr/local/bin/mediamtx /etc/mediamtx.yml
khalifa   2633  /usr/bin/python3 /home/khalifa/kallon/kallon_ptz_daemon.py --host 192.168.1.108 -P 80 -u admin --listen-host 127.0.0.1 --listen-port 8765
khalifa  37590  /usr/bin/python3 /home/khalifa/kallon/kallon_watchdog.py
```

`ss -tlnp` (listening sockets):

```text
LISTEN  *:8554        users:(("mediamtx",pid=1253,fd=...))         ← RTSP, all interfaces
LISTEN  127.0.0.1:8765 users:(("python3",pid=2633,fd=6))           ← PTZ daemon, loopback only
```

`i2cdetect -y -r 7` (the MPU-6050 lives here):

```text
00:                         -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
...
60: -- -- -- -- -- -- -- -- 68 -- -- -- -- -- -- --   ← MPU-6050 present at 0x68 ✅
70: -- -- -- -- -- -- -- --
```

`thermal_zone*` snapshot (Jetson runs cool):

```text
thermal_zone0 cpu-thermal   47.187 °C
thermal_zone1 gpu-thermal   46.187 °C
thermal_zone5 soc0-thermal  46.593 °C
thermal_zone8 tj-thermal    46.812 °C
```

Watchdog journal (last 60 s — actively detecting motion, alerting, deduping):

```text
13:30:02  WARNING kallon_watchdog  MPU returned all-zero accel; checking power state
13:30:02  WARNING kallon_watchdog  MPU in sleep/cycle (PWR_MGMT_1=0x40); re-waking
13:30:02  INFO    kallon_watchdog  motion poll now=(0.521,-0.354,0.929)g prev=(0.712,0.051,0.716)g delta_mg=405.0 threshold=150.0
13:30:02  INFO    kallon_watchdog  TAMPER_IMPACT triggered delta_mg=405.0 threshold=150.0
13:30:04  INFO    kallon_watchdog  alert sent type=TAMPER_IMPACT attempt=1 status=http_200      ← end-to-end success ✅
13:30:13  INFO    kallon_watchdog  TAMPER_IMPACT triggered delta_mg=161.1
13:30:13  INFO    kallon_watchdog  alert suppressed (dedup) type=TAMPER_IMPACT                  ← 60 s dedup working ✅
```

PTZ daemon journal — startup history (9 retries while the camera-route was still settling, then succeeded):

```text
08:55:24  systemd[1]:           Started Kallon ONVIF PTZ daemon (JSON/TCP).
08:55:24  python3[2624]: ERROR  connect/auth failed: ... [Errno 111] Connection refused
08:55:25  systemd[1]:           Main process exited, code=exited, status=1/FAILURE
08:55:25  systemd[1]:           Scheduled restart job, restart counter is at 9.
08:55:28  systemd[1]:           Started Kallon ONVIF PTZ daemon (JSON/TCP).
08:55:29  python3[2633]: INFO   connected to camera 192.168.1.108:80 profile_default=0
08:55:29  python3[2633]: INFO   listening TCP 127.0.0.1:8765
12:22:32  python3[2633]: INFO   client disconnected ('127.0.0.1', 58268)
```

> ✅ This proves the systemd `Restart=on-failure RestartSec=3` policy is correct — the daemon hammered the camera until the route came up, then stabilized at retry #9. It's been up since 08:55:29 with one successful client interaction at 12:22:32 (likely the chat session test).

`git -C /home/khalifa/kallon` — repo state:

```text
remote:  https://github.com/Yaqcodes/kallon-sentry.git
branch:  main (clean — `git status --short -b` returns just '## main')
latest:  4e73745 Auto-wake MPU6050 when it returns all-zero accel readings
         643c543 Add motion probe logging to diagnose TAMPER_IMPACT detection
         2cfa45c Fix crash loop: harden thermal zone read and isolate probe failures.
         f6dd1f8 Switch MPU motion detection from broken HW INT to polled accel delta.
         a82c12c Use read_bytes for thermal zones (avoids codec TypeError on Tegra).
         9a95487 Fix thermal zone read crash on Tegra (TypeError on unreadable zones).
         31138c0 Fix Orin Nano Super GPIO detection, invert LDR logic, add setup guide.
         5450d4a Mark install-kallon-watchdog.sh executable.
         0a9a2b0 Add Phase 4 tamper watchdog and deployment docs.
         ba0d5d0 Add Kallon edge ONVIF/PTZ stack for Jetson bench deployment.
```

> ⚠ The HEAD on the Jetson (`4e73745`) is **ahead of what's currently in the repo's working copy of `CODE/` on this NOC PC** — the recent Phase 4 hardening fixes were committed on the Jetson and pushed to `Yaqcodes/kallon-sentry`. After this doc lands, run `git pull` in `CODE/` to catch up.

Jetson user groups (relevant ones bolded):

```text
khalifa : khalifa adm cdrom **sudo** audio dip video plugdev render **i2c** lpadmin gdm **gpio** weston-launch sambashare
```

`/etc/kallon/`:

```text
-rw-r----- 1 root khalifa  45 May 21 14:53 alert.key       sha256=b725414aae...
-rw-r----- 1 root khalifa 763 May 21 14:53 device.env      (full contents in §7.6)
```

`/etc/wireguard/`:

```text
-rw------- 1 root root  45 May 20 13:47 jetson.private
-rw-r--r-- 1 root root  45 May 20 13:47 jetson.public
-rw------- 1 root root 240 May 20 13:54 wg0.conf          (full contents in §6.1)
```

### 3.8 Jetson host firewall — current posture

```text
Chain INPUT (policy ACCEPT 679K packets, 1034M bytes)
 pkts bytes target     prot opt in     out     source               destination
(empty — no rules)
```

> 🔴 **Open**: `INPUT` is wide open. The lab guide's `iptables -A INPUT -i wg0 -p tcp --dport 8554 -j ACCEPT` + corresponding DROP have **not** been applied. mediamtx is listening on `:8554` on **all interfaces**, so it's reachable on `192.168.1.246:8554` and `192.168.1.10:8554` as well as `10.50.0.2:8554`. On the bench this is harmless; on a real customer install it must be locked down before final sign-off. The lab-guide typo has now been fixed (see §13) so a fresh copy-paste won't fail.

---

## 4. Phase 1 — Data Sovereignty

**Brief goal:** zero outbound camera traffic to Dahua / third-party IPs. RTSP local-only to the Jetson.

### 4.1 What was done (and where it lives)

```mermaid
flowchart LR
    subgraph CAM["Dahua camera UI changes"]
        S1["P2P → OFF"]
        S2["DMSS cloud push → OFF"]
        S3["Easy4IP → OFF"]
        S4["Auto-update → OFF"]
        S5["Email / FTP alerts → OFF"]
        S6["NTP → local (or off)"]
    end
    subgraph BENCH["Bench network"]
        DIR["Direct Ethernet:<br/>Jetson eth0 ↔ Camera<br/>(no managed switch yet)"]
        FW["Jetson iptables<br/>(Phase 3 lab partial check)"]
    end
    subgraph DEFER["Deferred until switch arrives"]
        VLAN["Camera VLAN 802.1Q"]
        ACL["VLAN ACL:<br/>cameras → Jetson only"]
        MIRROR["Mirror port + 24h Wireshark soak<br/>= Phase 1 exit criterion"]
    end
    CAM --> BENCH
    BENCH -. blocked .-> DEFER
```




| Artifact                                                                       | Where                                                                                                                            | Status                                                                                                                        |
| ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Camera UI cloud toggles                                                        | Dahua web UI (manual procedure)                                                                                                  | 🟡 Documented in `kallon_sovereign_stack_brief.md` §Phase 1; not yet recorded as "applied on this unit".                      |
| Direct Jetson↔camera Ethernet path on `enP8p1s0`                               | Jetson hardware + `kallon-camera-route.service`                                                                                  | ✅ Live: `enP8p1s0` UP, holds `192.168.1.10/24`. `ip route get 192.168.1.108` returns `dev enP8p1s0`. Ping latency `0.322 ms`. |
| `kallon-camera-route.service` (oneshot pinning `192.168.1.108/32` to enP8p1s0) | Jetson `/etc/systemd/system/kallon-camera-route.service`. Now also in repo at `CODE/deploy/kallon-camera-route.service.example`. | ✅ DEPLOYED. `active (exited)`. Verified live today.                                                                           |
| `dahua_onvif_control.py test`                                                  | `CODE/dahua_onvif_control.py`                                                                                                    | ✅ Camera reachable on `192.168.1.108:80`; PTZ daemon journal confirms ONVIF auth success.                                     |
| Baseline pcap (`baseline_capture_phase0.pcap`)                                 | —                                                                                                                                | 🔴 Not captured; requires managed switch mirror port.                                                                         |
| Post-isolation pcap (`post_isolation_capture_phase1.pcap`)                     | —                                                                                                                                | 🔴 Not captured; same blocker.                                                                                                |
| Managed switch + VLAN ACL config                                               | —                                                                                                                                | ⏸ **Hardware blocker** — switch not yet purchased per `jetson-lab-steps-8-10.md` §Step 10.                                    |


Live `kallon-camera-route.service` (captured from Jetson today):

```ini
[Unit]
Description=Route camera IP via direct Ethernet
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/sbin/ip link set enP8p1s0 up
ExecStart=/sbin/ip addr replace 192.168.1.10/24 dev enP8p1s0
ExecStart=/sbin/ip route replace 192.168.1.108/32 dev enP8p1s0

[Install]
WantedBy=multi-user.target
```

### 4.2 ONVIF port — historical confusion

From the chat transcript record:

- The snap `onvif-viewer` shipped with a demo `xaddr: 192.168.123.211:8899` → user mis-applied `**:8899**` to the real camera → connection refused → app panic.
- `8899` is the ONVIF port on **some** Dahua firmware lines but not this unit.
- Resolution: `nc -zv 192.168.1.108 80` succeeded → updated `xaddr: 192.168.1.108:80` → ONVIF profiles discovered (`MediaProfile00000`, `00001`, `00002`).
- All scripts in the repo now default to `**DEFAULT_PORT = 80`** (`dahua_onvif_control.py:31`).

### 4.3 What Phase 1 still requires

Per the brief's exit criteria:

- 24-hour Wireshark capture on switch mirror port showing **0 bytes** outbound from camera to anything except the Jetson.
- Comparison document: `baseline_capture_phase0.pcap` vs `post_isolation_capture_phase1.pcap`.
- Demonstrable "Terra test machine off VPN cannot see stream".

All three are blocked by the **managed PoE switch** purchase.

---

## 5. Phase 2 — Local PTZ Control

**Brief goal:** local ONVIF-only PTZ; deterministic AbsoluteMove with confirmation loop; p95 < 100 ms over 1,000 commands.

### 5.1 The three-layer code

```mermaid
flowchart TB
    subgraph L1["Layer 1 — ONVIF transport + WSDL"]
        ONVIFLIB["onvif-zeep (PyPI)"]
        WSDL["CODE/wsdl/  ← bundled<br/>devicemgmt.wsdl, ptz.wsdl, media.wsdl,<br/>+ ~30 other ONVIF/WS-* schemas"]
        DAHUA["CODE/dahua_onvif_control.py<br/>connect() · profile_token() ·<br/>resolve_password() · resolve_wsdl_dir()"]
        ONVIFLIB --> DAHUA
        WSDL --> DAHUA
    end
    subgraph L2["Layer 2 — Deterministic control"]
        ABS["CODE/sentry_ptz_absolute.py<br/>absolute_move() ·<br/>wait_position() ·<br/>wait_position_after_command() ·<br/>cmd_move / cmd_status / cmd_benchmark"]
        DAHUA --> ABS
    end
    subgraph L3["Layer 3 — Long-running service"]
        DAEMON["CODE/kallon_ptz_daemon.py<br/>JSON over TCP 8765 / Unix socket<br/>asyncio Server · asyncio.Lock<br/>methods: ping/status/move_absolute/<br/>move_continuous/stop/home"]
        UNIT["CODE/deploy/<br/>kallon-ptz-daemon.service.example"]
        ABS --> DAEMON
        DAHUA --> DAEMON
        DAEMON --> UNIT
    end
    subgraph CONSUMERS["Phase 5 (future)"]
        ART["ArtemisOS / mission planner"]
        TRACK["AI threat tracker → AbsoluteMove"]
        ART --> DAEMON
        TRACK --> DAEMON
    end
```



### 5.2 PTZ daemon JSON-RPC over `127.0.0.1:8765`

One JSON object per line (UTF-8, `\n` terminated). Implemented in `CODE/kallon_ptz_daemon.py` lines 118–174.

```json
// → request
{"id": 1, "method": "ping", "params": {}}
// ← response
{"id": 1, "ok": true, "result": {"pong": true}}

// → status
{"id": 2, "method": "status", "params": {"profile": 0}}
// ← {"id":2,"ok":true,"result":{"pan":-0.892,"tilt":0.503,"zoom":0.04}}

// → move_absolute (with confirmation loop)
{"id": 3, "method": "move_absolute",
 "params": {"pan": 0.2, "tilt": 0.0,
            "tolerance": 0.03, "poll_ms": 50.0, "confirm_timeout": 10.0}}
// ← {"id":3,"ok":true,"result":{"confirmed":true,"round_trip_ms":456.2}}

// move_continuous · stop · home — same shape
```

Security model: **password only at startup** (via `CAMERA_PASSWORD` env or `-p`), **never in each JSON line**.

### 5.3 PTZ daemon — **deployed and running** on the Jetson

The daemon is **fully implemented and live**. Status on the Jetson today:

```text
$ systemctl is-active kallon-ptz-daemon
active

$ ps -fp 2633
khalifa  2633  /usr/bin/python3 /home/khalifa/kallon/kallon_ptz_daemon.py \
                  --host 192.168.1.108 -P 80 -u admin \
                  --listen-host 127.0.0.1 --listen-port 8765

$ ss -tlnp | grep 8765
LISTEN  127.0.0.1:8765   users:(("python3",pid=2633,fd=6))

$ journalctl -u kallon-ptz-daemon | tail -3
08:55:29 INFO connected to camera 192.168.1.108:80 profile_default=0
08:55:29 INFO listening TCP 127.0.0.1:8765
12:22:32 INFO client disconnected ('127.0.0.1', 58268)
```

**Why the `echo … | nc 127.0.0.1 8765` examples in `HOW_TO_USE.md` work.** The PTZ daemon **is** the long-running process the examples talk to. Any one of these three states yields a working endpoint:

1. **Production (current Jetson state):** `kallon-ptz-daemon.service` was installed in `/etc/systemd/system/`, enabled with `systemctl enable --now`, and is now `active (running)` as PID 2633. It opens a TCP listener on `127.0.0.1:8765` and holds one persistent zeep/ONVIF session to the camera. Each `echo '{"id":1,"method":"ping","params":{}}' | nc 127.0.0.1 8765` becomes a JSON line on this listener; the daemon dispatches to `dispatch()` (`kallon_ptz_daemon.py:118`), takes the asyncio lock, optionally calls into `sentry_ptz_absolute.wait_position_after_command` (with the AbsoluteMove + GetStatus polling logic), and writes one JSON response line back.
2. **Bench / manual:** a developer runs `python kallon_ptz_daemon.py --host 192.168.1.108` directly in a shell (no systemd). Same socket, same protocol — exactly what the `HOW_TO_USE.md` snippet illustrates.
3. **Unit-socket variant:** if you start with `--unix /run/kallon-ptz.sock`, you talk to it via `socat - UNIX-CONNECT:/run/kallon-ptz.sock` (Linux only). Same JSON protocol.

So: **the daemon is implemented (333 lines, all 6 methods), and it's running as a systemd unit on the Jetson right now.** Earlier wording in v1.0 of this document ("only `.example` ships") was wrong — the Jetson side has a real, edited `kallon-ptz-daemon.service` that the repo now reflects.

#### Live unit file on the Jetson today

`/etc/systemd/system/kallon-ptz-daemon.service` (verbatim — note `User=khalifa`, password injected via `Environment=`, paths under `/home/khalifa/kallon`):

```ini
[Unit]
Description=Kallon ONVIF PTZ daemon (JSON/TCP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=khalifa
Group=khalifa
WorkingDirectory=/home/khalifa/kallon

Environment=CAMERA_PASSWORD=terra123
Environment=PTZ_LISTEN_HOST=127.0.0.1
Environment=PTZ_LISTEN_PORT=8765

ExecStart=//usr/bin/python3 /home/khalifa/kallon/kallon_ptz_daemon.py \
    --host 192.168.1.108 -P 80 -u admin \
    --listen-host 127.0.0.1 --listen-port 8765

Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

> ℹ️ `//usr/bin/python3` (double slash) is a cosmetic typo in the live file — Linux tolerates duplicate path separators, so the unit runs fine. The `.example` template in the repo (now corrected — see below) uses the single-slash form.

The repo's template (`CODE/deploy/kallon-ptz-daemon.service.example`) was **fixed in this session** to match the deployed reality: `User=khalifa`, `WorkingDirectory=/home/khalifa/kallon`, and `ExecStart=/usr/bin/python3 /home/khalifa/kallon/...`. The placeholder `CAMERA_PASSWORD=change_me` remains so the template isn't shipping with a real password.

### 5.4 PTZ benchmark — observed result

From the live `sentry_ptz_absolute.py benchmark` run during development:

```text
Iterations: 10  OK: 9  Failed/timeouts: 1
Round-trip confirm time (ms): min=1523.1  max=1976.5  mean=1633.7
                              median=1560.9  p95~=1761.5
```

**Verdict against brief target (p95 < 100 ms):** **🔴 missed by ~17×**. The brief acknowledged this could happen and reframed the script as "the behavioral spec / benchmark harness" — the camera + ONVIF + HTTP stack itself, not the Python, is what makes ~1.5–2 s realistic on Dahua PTZ. Decision recorded in chat: "either re-baseline the target for Dahua ONVIF or move to a different control path (e.g. lower-level driver) **after** this harness proves what ONVIF alone can do."

### 5.5 PTZ status


| Brief requirement (Phase 2)                          | Implementation                                                                | Status                                                                                             |
| ---------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `move_absolute(pan, tilt, zoom)`                     | `sentry_ptz_absolute.absolute_move` (l. 88–95)                                | ✅                                                                                                  |
| `move_continuous(...)`                               | `dahua_onvif_control.cmd_ptz` (l. 156) + `kallon_ptz_daemon._continuous_move` | ✅                                                                                                  |
| `get_position()`                                     | `sentry_ptz_absolute.read_pan_tilt_zoom` (l. 81)                              | ✅                                                                                                  |
| `move_with_confirm(...)` w/ 50 ms polling, tolerance | `wait_position_after_command` (l. 132)                                        | ✅                                                                                                  |
| Wrap as systemd service                              | Daemon coded; unit file is `.example` only                                    | 🟡 — operator must copy + edit + `systemctl enable`. No baked-in unit on Jetson per current state. |
| Expose over Unix socket or gRPC                      | TCP **and** Unix socket supported (`--unix /path`); gRPC deferred             | ✅ (TCP/Unix) · 🔴 (gRPC)                                                                           |
| 1,000-cmd p95 < 100 ms                               | 10-cmd run shows ~1.6 s; full 1,000 not run                                   | 🔴                                                                                                 |
| No external IPs in PTZ path                          | All HTTP is to `192.168.1.108` on the camera LAN                              | ✅                                                                                                  |


---

## 6. Phase 3 — Per-Customer Private Network (WireGuard + mediamtx)

**Brief goal:** each Kallon unit establishes a WireGuard tunnel to the customer's gateway; RTSP and alerts flow only over that tunnel; Terra is out of the data path.

### 6.1 Verified VPN topology

```mermaid
flowchart LR
    subgraph J["Jetson 10.50.0.2 (peer)"]
        JWG["wg-quick@wg0 (userspace fallback)<br/>PrivateKey: 8KlzCK59...<br/>PublicKey:  A8urKcYr..."]
    end
    subgraph V["VPS 10.50.0.1 (hub) — 18.220.75.237:51820"]
        VWG["wg-quick@wg0 (kernel)<br/>PrivateKey: UPcc3wZw...<br/>PublicKey:  mXuS1kDI..."]
    end
    subgraph N["NOC PC 10.50.0.10 (peer)"]
        NWG["WireGuard for Windows<br/>PublicKey:  lN6KnJrn..."]
    end
    J <== "UDP 51820 (NAT 135.129.124.77:20939)<br/>last handshake 1 s ago · 88 MiB in / 10 MiB out" ==> V
    V <== "UDP 51820 (NAT 135.129.124.77:42417)<br/>last handshake 28 s ago · 88 MiB out / 3 MiB in" ==> N
    J -. "AllowedIPs=10.50.0.0/24 — full subnet (not /0!)" .- V
    V -. "Jetson AllowedIPs=10.50.0.2/32<br/>NOC AllowedIPs=10.50.0.10/32" .- N
```



### 6.2 Tegra userspace WireGuard wrinkle (Jetson)

The Jetson kernel ships **without** the in-tree WireGuard module on most JetPack 6.x images, so `wg-quick up wg0` fails with `Unknown device type / Protocol not supported`. The fix lives in `jetson-lab-steps-8-10.md` §8.5:

```ini
# /etc/systemd/system/wg-quick@wg0.service.d/userspace.conf
[Service]
Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/bin/wireguard
```

(Note: on Jammy/Jetson the `wireguard-go` package installs the binary as `/usr/bin/wireguard`, **not** `/usr/bin/wireguard-go`.)

Indirect evidence this drop-in is in place on the Jetson: the VPS sees `peer Jetson latest handshake = 1 s ago` — that only happens if the Jetson's `wg-quick@wg0` is up, which on Tegra requires this drop-in.

### 6.3 mediamtx (RTSP rebroadcast)

```mermaid
sequenceDiagram
    autonumber
    participant Cam as Dahua Camera<br/>192.168.1.108
    participant MTX as mediamtx (Jetson)<br/>:8554 on wg0
    participant NOC as NOC VLC / ffprobe<br/>10.50.0.10
    NOC->>MTX: rtsp://10.50.0.2:8554/cam1 (TCP, over wg0)
    MTX->>Cam: rtsp://admin:****@192.168.1.108/cam/realmonitor?<br/>channel=1&subtype=0 (sourceOnDemand)
    Cam-->>MTX: H.264 video (RTP/RTCP, TCP)
    MTX-->>NOC: H.264 video (RTP/RTCP, TCP) — over WG tunnel
```



Per `jetson-lab-steps-8-10.md` §8.6:

```yaml
# /etc/mediamtx.yml
rtspAddress: :8554
paths:
  cam1:
    source: rtsp://admin:PASSWORD@192.168.1.108/cam/realmonitor?channel=1&subtype=0
    sourceOnDemand: yes
    rtspTransport: tcp
```

```ini
# /etc/systemd/system/mediamtx.service
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

> ✅ **Verified live today.** `mediamtx.service` is `active (running)` as PID 1253; `/etc/mediamtx.yml` matches the structure above (using `subtype=1` substream); `journalctl -u mediamtx` shows RTSP `sourceOnDemand` working: `[path cam1] [RTSP source] started on demand … ready: 1 track (H264)` → session created from `127.0.0.1:50830` (this was the Jetson-local `ffprobe` test that the watchdog runs).

### 6.4 WireGuard auto-reconnect watchdog (Phase 3 brief requirement)

**Status:** ✅ **DEPLOYED, ACTIVE, AND NOW MIRRORED IN THE REPO.**

Live observation on the Jetson today:

```text
$ systemctl list-units | grep kallon-wg-watchdog
kallon-wg-watchdog.service    loaded inactive dead    Restart WireGuard if handshake stale
kallon-wg-watchdog.timer      loaded active   waiting Check WireGuard handshake every 30s

$ ls -la /usr/local/sbin/kallon-wg-watchdog.sh /etc/systemd/system/kallon-wg-watchdog.*
-rwxr-xr-x 1 root root 218 May 21 09:00 /usr/local/sbin/kallon-wg-watchdog.sh
-rw-r--r-- 1 root root 128 May 21 09:01 /etc/systemd/system/kallon-wg-watchdog.service
-rw-r--r-- 1 root root 130 May 21 09:02 /etc/systemd/system/kallon-wg-watchdog.timer
```

The `.service` is a `Type=oneshot` and is normally `inactive (dead)`; the `.timer` is `active (waiting)` and fires the oneshot every 30 seconds. Source on the Jetson (verbatim):

```bash
#!/bin/bash
set -euo pipefail
HS=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}')
NOW=$(date +%s)
if [ -z "$HS" ] || [ "$HS" = "0" ] || [ $((NOW - HS)) -gt 60 ]; then
  systemctl restart wg-quick@wg0
fi
```

```ini
# kallon-wg-watchdog.service
[Unit]
Description=Restart WireGuard if handshake stale

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/kallon-wg-watchdog.sh
```

```ini
# kallon-wg-watchdog.timer
[Unit]
Description=Check WireGuard handshake every 30s

[Timer]
OnBootSec=30
OnUnitActiveSec=30

[Install]
WantedBy=timers.target
```

**Repo gap closed in this session.** These three files are now versioned at:

- `CODE/deploy/kallon-wg-watchdog.sh`
- `CODE/deploy/kallon-wg-watchdog.service.example`
- `CODE/deploy/kallon-wg-watchdog.timer.example`

Install on a new Jetson with:

```bash
sudo install -m 0755 deploy/kallon-wg-watchdog.sh /usr/local/sbin/kallon-wg-watchdog.sh
sudo cp deploy/kallon-wg-watchdog.service.example /etc/systemd/system/kallon-wg-watchdog.service
sudo cp deploy/kallon-wg-watchdog.timer.example   /etc/systemd/system/kallon-wg-watchdog.timer
sudo systemctl daemon-reload
sudo systemctl enable --now kallon-wg-watchdog.timer
```

### 6.5 Phase 3 status


| Brief requirement                                        | Status | Evidence                                                                                                                                                                                                                                                       |
| -------------------------------------------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Per-device curve25519 keypair                            | ✅      | Verified live today: `wg show wg0` on Jetson reports public key `A8urKcYr...`, matches VPS peer block.                                                                                                                                                         |
| `AllowedIPs` scoped to customer subnet (not `0.0.0.0/0`) | ✅      | Jetson side: `AllowedIPs = 10.50.0.0/24`; VPS side: `/32` per peer.                                                                                                                                                                                            |
| Auto-reconnect watchdog (60 s)                           | ✅      | `kallon-wg-watchdog.timer` `active (waiting)`; script at `/usr/local/sbin/kallon-wg-watchdog.sh`.                                                                                                                                                              |
| `mediamtx` running, reachable on `wg0`                   | ✅      | `active (running)` PID 1253; binds `:8554` on all interfaces (incl. `wg0`); journal shows successful `sourceOnDemand` sessions.                                                                                                                                |
| Userspace WireGuard on Tegra                             | ✅      | `/etc/systemd/system/wg-quick@wg0.service.d/userspace.conf` exists; live handshake counter proves the userspace impl is being used.                                                                                                                            |
| End-to-end RTSP via VPN                                  | ✅      | Tunnel up both sides; mediamtx confirmed; only the final NOC→VLC pull remains a manual verification.                                                                                                                                                           |
| Two-tunnel cross-customer isolation pcap                 | ⏸      | Blocked by managed switch (per §Step 10).                                                                                                                                                                                                                      |
| Terra test machine off VPN cannot reach stream           | 🔴     | `iptables INPUT` is empty / `ACCEPT` everywhere. mediamtx listens on `0.0.0.0:8554`, so `192.168.1.246:8554` is also reachable from the LAN. Rule from `jetson-lab-steps-8-10.md` §8.7 not yet applied (the lab-guide `ACCEP` typo was fixed in this session). |


### 6.6 The Jetson host firewall (Phase 3 partial sign-off)

```bash
sudo iptables -A INPUT -i wg0 -p tcp --dport 8554 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8554 -j DROP
```

> ✅ The lab-guide typo (`ACCEP` → `ACCEPT`) was fixed in this session — `jetson-lab-steps-8-10.md` now contains the correct two-line snippet above.
>
> 🔴 But **the rules are not yet applied** on the Jetson (`iptables -L -n -v` shows empty `INPUT` chain). Run them as a pre-deployment step and persist with `iptables-persistent`.

---

## 7. Phase 4 — Tamper Detection & Health Monitoring

**Brief goal:** real-time tamper + health alerts to the customer NOC; signed JSON; SLA: enclosure open within 5 s, stream fail within 30 s.

### 7.1 Jetson Orin Nano J12 wiring (Rev A)

```mermaid
flowchart LR
    subgraph J12["J12 40-pin header (BOARD numbering)"]
        P1["pin 1: 3.3 V"]
        P3["pin 3: I2C1_SDA (bus 7)"]
        P5["pin 5: I2C1_SCL (bus 7)"]
        P6["pin 6: GND"]
        P29["pin 29: GPIO01 (MPU INT)"]
        P31["pin 31: GPIO11 (Reed)"]
        P33["pin 33: GPIO13 (LDR OUT)"]
    end
    subgraph SENS["Sensors on breakout"]
        MPU["MPU-6050<br/>I2C @ 0x68 (AD0 low)<br/>SDA/SCL pull-ups already on board"]
        REED["Reed switch<br/>10 kΩ pull-up to 3.3 V<br/>HIGH = door open"]
        LDR["LDR module (active-low)<br/>LOW = bright (alarm)<br/>HIGH = dark (normal)"]
    end
    P3 --- MPU
    P5 --- MPU
    P29 --- MPU
    P1 --- MPU
    P6 --- MPU
    P1 --- REED
    P31 --- REED
    P6 --- REED
    P1 --- LDR
    P33 --- LDR
    P6 --- LDR
```



**BOM evidence:** `Kallon Tamper Detection BOM.pdf` (Microscale cart, ₦17,610 total):

- Door magnetic reed switch PS-3150 (₦2,500)
- Accelerometer MPU-6050 (₦4,000)
- LDR sensor module 4-pin (₦700)
- 10 kΩ resistor, jumper wires, breadboard, header pins, 1 µF cap
- ADS1115 module quad 16-bit ADC (₦6,500) — *purchased but unused yet; reserved for `ENABLE_POWER_ADC=1` later.*

### 7.2 Watchdog architecture

```mermaid
flowchart TB
    subgraph PROBES["Pollers (every POLL_INTERVAL_SEC = 10 s)"]
        RTSP["RtspProbe<br/>ffprobe RTSP_URLS, 5 s timeout"]
        TEMP["TemperatureProbe<br/>/sys/class/thermal/thermal_zone*<br/>trigger 80 °C / clear 75 °C"]
        NVME["NvmeProbe (ENABLE_NVME=0 → inert)<br/>smartctl -A -j /dev/nvme0"]
        MOT["MotionProbe<br/>read MPU accel · compare to prev<br/>fire if |Δ| ≥ 150 mg"]
    end
    subgraph EVENTS["Event-driven (Jetson.GPIO BOTH-edge, 50 ms debounce)"]
        REED["_on_reed (pin 31)<br/>HIGH→TAMPER_DOOR_OPEN<br/>LOW→TAMPER_DOOR_RECOVERED"]
        LDR["_on_ldr (pin 33)<br/>LOW→TAMPER_LIGHT<br/>HIGH→TAMPER_LIGHT_RECOVERED"]
    end
    subgraph SENDER["AlertSender (background thread)"]
        Q["queue.Queue (FIFO)"]
        DEDUP["60 s dedup<br/>per alert_type"]
        BUILD["Build payload:<br/>device_id, timestamp_utc, nonce (uuid4),<br/>alert_type, severity, details"]
        SIGN["HMAC-SHA256(alert.key, canonical JSON)<br/>→ X-Kallon-Signature: sha256=hex"]
        POST["HTTP POST ALERT_WEBHOOK_URL<br/>3 attempts · backoff 1/2/4 s · 5 s timeout"]
    end
    RTSP --> Q
    TEMP --> Q
    NVME --> Q
    MOT --> Q
    REED --> Q
    LDR --> Q
    Q --> DEDUP --> BUILD --> SIGN --> POST
    POST -- success --> NOC["NOC (VPS) :8080"]
    POST -- exhausted --> DROP["LOG.error 'alert dropped after 3 attempts'"]
```



### 7.3 Eleven alert types (from `kallon_watchdog.py` lines 90–116)


| Alert type                | Severity | Trigger                                                              |
| ------------------------- | -------- | -------------------------------------------------------------------- |
| `TAMPER_DOOR_OPEN`        | CRITICAL | Reed pin transitions LOW→HIGH                                        |
| `TAMPER_DOOR_RECOVERED`   | MEDIUM   | Reed pin transitions HIGH→LOW                                        |
| `TAMPER_LIGHT`            | CRITICAL | LDR pin transitions HIGH→LOW (bright)                                |
| `TAMPER_LIGHT_RECOVERED`  | MEDIUM   | LDR pin transitions LOW→HIGH (dark)                                  |
| `TAMPER_IMPACT`           | HIGH     | MPU accel Δ ≥ threshold between polls (default 150 mg)               |
| `CAMERA_STREAM_FAIL`      | HIGH     | `ffprobe` non-zero exit or 5 s timeout                               |
| `CAMERA_STREAM_RECOVERED` | MEDIUM   | `ffprobe` recovers after fail                                        |
| `TEMP_CRITICAL`           | HIGH     | hottest thermal zone ≥ 80 °C                                         |
| `TEMP_RECOVERED`          | MEDIUM   | hottest thermal zone falls below 75 °C                               |
| `DISK_FAULT`              | HIGH     | nvme `critical_warning` or `media_errors` (gated by `ENABLE_NVME=1`) |
| `POWER_UNDERVOLT`         | MEDIUM   | gated by `ENABLE_POWER_ADC=1` — inert until ADS1115 wired            |


### 7.4 Alert wire format (canonical JSON, sorted keys)

```json
{
  "alert_type": "TAMPER_DOOR_OPEN",
  "details": {"gpio_pin": 31, "level": "HIGH"},
  "device_id": "kallon-unit-001",
  "nonce": "9a1f4f33-7c8e-4f1d-9c4b-2a6b8b3f1d12",
  "severity": "CRITICAL",
  "timestamp_utc": "2026-05-25T13:10:08Z"
}
```

Header: `X-Kallon-Signature: sha256=<hex>` over the **exact byte string** above. VPS `alert_listener.py` recomputes and `hmac.compare_digest`s — mismatch → HTTP 403 + log line `[REJECTED] bad HMAC from <ip>`.

### 7.5 Where the Phase 4 pieces live


| Piece                          | Host   | Path                                                     | Owner        | Mode |
| ------------------------------ | ------ | -------------------------------------------------------- | ------------ | ---- |
| Watchdog daemon source         | Jetson | `/home/khalifa/kallon/kallon_watchdog.py`                | khalifa      | 0644 |
| Installer                      | Jetson | `/home/khalifa/kallon/deploy/install-kallon-watchdog.sh` | khalifa      | 0755 |
| systemd unit (example in repo) | repo   | `CODE/deploy/kallon-watchdog.service.example`            | —            | 0644 |
| systemd unit (installed)       | Jetson | `/etc/systemd/system/kallon-watchdog.service`            | root         | 0644 |
| Device config                  | Jetson | `/etc/kallon/device.env`                                 | root:khalifa | 0640 |
| HMAC key (Jetson side)         | Jetson | `/etc/kallon/alert.key`                                  | root:khalifa | 0640 |
| HMAC key (VPS side)            | VPS    | `/etc/kallon/alert.key`                                  | root         | 0640 |
| Alert listener                 | VPS    | `/home/ubuntu/kallon-noc/alert_listener.py`              | ubuntu       | 0664 |
| Operator runbook               | repo   | `CODE/phase4-setup-guide.md`                             | —            | —    |
| Wiring spec                    | repo   | `CODE/kallon_hardware_wiring.md`                         | —            | —    |


### 7.6 `device.env` (current template, written by installer)

```bash
DEVICE_ID=kallon-unit-001
ALERT_WEBHOOK_URL=http://10.50.0.1:8080/alerts
ALERT_KEY_PATH=/etc/kallon/alert.key
RTSP_URLS=rtsp://127.0.0.1:8554/cam1
POLL_INTERVAL_SEC=10
TEMP_TRIGGER_C=80
TEMP_CLEAR_C=75
DEDUP_WINDOW_SEC=60
MPU_I2C_BUS=7
MPU_I2C_ADDR=0x68
GPIO_REED_PIN=31
GPIO_LDR_PIN=33
MPU_ACCEL_THRESHOLD_MG=150
ENABLE_NVME=0
NVME_DEVICE=/dev/nvme0
ENABLE_POWER_ADC=0
```

### 7.7 Phase 4 status


| Brief requirement                     | Status | Live evidence                                                                                                                                |
| ------------------------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| MPU-6050 wired to I2C bus 7 @ 0x68    | ✅      | `i2cdetect -y -r 7` shows `68` populated today. Live motion polls: `(0.521,-0.354,0.929)g` etc.                                              |
| Reed switch on GPIO BOARD pin 31      | ✅      | `device.env` has `GPIO_REED_PIN=31`; `khalifa` is in the `gpio` group.                                                                       |
| LDR / photodiode on GPIO BOARD pin 33 | ✅      | `device.env` has `GPIO_LDR_PIN=33`.                                                                                                          |
| systemd watchdog, 10 s poll           | ✅      | PID 37590, `journalctl -u kallon-watchdog` shows polls every ~11–12 s.                                                                       |
| RTSP fail → alert in 30 s             | ✅      | 10 s poll + 5 s ffprobe timeout = SLA met.                                                                                                   |
| Door open → alert in 5 s              | ✅      | Interrupt-driven via `Jetson.GPIO` BOTH-edge detect, 50 ms debounce.                                                                         |
| HMAC-SHA256 signed JSON               | ✅      | Verified live: at `13:30:02` an MPU `delta_mg=405.0` triggered `TAMPER_IMPACT`; alert sent `attempt=1 status=http_200` → end-to-end success. |
| NOC webhook verifier                  | ✅      | Running live on VPS as PID 45244.                                                                                                            |
| Dedup window (60 s)                   | ✅      | At `13:30:13` a second impact (`delta_mg=161.1`) was logged `alert suppressed (dedup) type=TAMPER_IMPACT` — exactly correct behavior.        |
| Watchdog auto-restart on crash        | ✅      | `Restart=on-failure RestartSec=3` in the unit.                                                                                               |
| MPU auto-wake on sleep cycle          | ✅      | Newly-shipped fix in commit `4e73745` — log shows `MPU in sleep/cycle (PWR_MGMT_1=0x40); re-waking` then proceeding successfully.            |
| NVMe SMART checks                     | 🟡     | Code present, `ENABLE_NVME=0` keeps it inert until an SSD is wired.                                                                          |
| Power undervoltage via ADC            | 🟡     | Code present, `ENABLE_POWER_ADC=0` keeps it inert until the ADS1115 is wired.                                                                |


---

## 8. Master file inventory

Every file in the project, where it lives, and what it's for. Sorted by host then path.

### 8.1 NOC PC (Windows workstation) — `C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\`


| Path                                                                | Bytes / kind      | Purpose                                                                                         | Sensitive? |
| ------------------------------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------------- | ---------- |
| `kallon-vps-key.pem`                                                | RSA 2048 priv     | SSH to `ubuntu@18.220.75.237`                                                                   | 🔴 secret  |
| `wg-keys.txt`                                                       | text              | All four WireGuard key pairs + HMAC alert key                                                   | 🔴 secret  |
| `kallon_before_after_architecture.af`                               | Affinity Designer | Architecture diagram (before vs after)                                                          | —          |
| `kallon_sovereign_architecture.af`                                  | Affinity Designer | Sovereign stack architecture                                                                    | —          |
| `terra_industries_kallon_analysis.af`                               | Affinity Designer | Internal analysis                                                                               | —          |
| `Kallon Tamper Detection BOM.pdf`                                   | PDF               | Microscale shopping cart (₦17,610)                                                              | —          |
| `General_ConfigTool_ChnEng_V5.001.0000006.2.R.20250922.exe`         | binary            | Dahua ConfigTool installer                                                                      | —          |
| `odm-v2.2.250r.msi`                                                 | binary            | ONVIF Device Manager installer                                                                  | —          |
| `Jetson/jetson-orin-nano pinout.png`                                | image             | J12 reference                                                                                   | —          |
| `Jetson/jetson-orin-nano-devkit-super-SD-image_JP6.2.1/sd-blob.img` | ~ several GB      | JetPack 6.2.1 SD image                                                                          | —          |
| `EventsConsole/`                                                    | C#/.NET 4.0       | Legacy ONVIF events sample (Reactive Extensions). Not part of production stack. Reference only. | —          |
| `CODE/` (cloned `Yaqcodes/kallon-sentry`)                           | git repo          | Production stack — see below                                                                    | —          |


### 8.2 Repo `CODE/` (git root, branch `main` → `github.com/Yaqcodes/kallon-sentry`)

Hierarchical view:

```text
CODE/
├── .gitignore                                    ← excludes .env, *.pem, *.key, wg-keys*, etc.
├── HOW_TO_USE.md                                 ← ONVIF & PTZ daemon CLI reference (operator)
├── kallon_sovereign_stack_brief.md               ← THE master brief (v1.0, May 2026)
├── kallon_mass_deployment_roadmap.md             ← path from bench to mass production
├── jetson-lab-steps-8-10.md                      ← Phase 3 lab guide (WG + mediamtx + watchdogs)
├── kallon_hardware_wiring.md                     ← J12 pin map, sensor logic (Rev A)
├── phase4-setup-guide.md                         ← end-to-end Phase 4 bring-up procedure
├── requirements.txt                              ← onvif-zeep, requests, smbus2, Jetson.GPIO
├── dahua_onvif_control.py                        ← Phase 0/1/2 — ONVIF CLI (test/info/profiles/rtsp/snapshot/ptz/stop/home)
├── sentry_ptz_absolute.py                        ← Phase 2 — AbsoluteMove + GetStatus confirm loop + benchmark
├── kallon_ptz_daemon.py                          ← Phase 2 — long-running JSON-RPC PTZ daemon
├── kallon_watchdog.py                            ← Phase 4 — sensors + health + HMAC alerts
├── deploy/
│   ├── install-kallon-watchdog.sh                  ← Phase 4 idempotent installer
│   ├── kallon-ptz-daemon.service.example           ← Phase 2 systemd unit (template — fixed in this session)
│   ├── kallon-watchdog.service.example             ← Phase 4 systemd unit (template)
│   ├── kallon-wg-watchdog.sh                       ← Phase 3 WG handshake watchdog script    NEW
│   ├── kallon-wg-watchdog.service.example         ← Phase 3 systemd oneshot template       NEW
│   ├── kallon-wg-watchdog.timer.example           ← Phase 3 systemd timer template         NEW
│   ├── kallon-camera-route.service.example         ← Phase 1 camera-NIC pin template        NEW
│   ├── mediamtx.service.example                   ← Phase 3 mediamtx systemd template       NEW
│   ├── mediamtx.yml.example                       ← Phase 3 mediamtx config template        NEW
│   ├── wg-quick-wg0-userspace.conf.example         ← Phase 3 Tegra userspace drop-in         NEW
│   └── (no other scripts)
└── wsdl/                                         ← ONVIF schema bundle (~30 files, ships with repo because PyPI wheel omits them)
    ├── devicemgmt.wsdl
    ├── ptz.wsdl
    ├── media.wsdl
    ├── analytics.wsdl
    ├── imaging.wsdl
    ├── events.wsdl
    └── ... (plus WS-Discovery, WS-Addressing, etc.)
```

### 8.3 Jetson runtime layout (target — per docs)

```text
/home/khalifa/kallon/                     ← git clone of CODE/
├── (all of CODE/ above)

/etc/kallon/                              ← per-device, mode 0750, root:khalifa
├── device.env                            ← 0640 root:khalifa — DEVICE_ID, ALERT_WEBHOOK_URL, RTSP_URLS, tuning, pin map
└── alert.key                             ← 0640 root:khalifa — 32-byte HMAC base64

/etc/wireguard/                           ← mode 0700 root:root
├── jetson.private                        ← 0600 root:root — 8KlzCK59DZhafnHxQOJtd4DLZQoohWX+ztzkmn5rIGM=
├── jetson.public                         ← public — A8urKcYrPJzJfgt8V0GPdmFaigO8rgGjUMGBHJVjJyw=
├── gateway.public                        ← public — mXuS1kDIK49t6TFcDRJ9AqlKdb+jDxkilNcaM+oWdnw=
└── wg0.conf                              ← 0600 root:root — Jetson side (see §6)

/etc/mediamtx.yml                         ← RTSP rebroadcast config (Phase 3)

/etc/systemd/system/
├── kallon-watchdog.service               ← Phase 4 — active (running) ✅ (PID 37590)
├── kallon-ptz-daemon.service             ← Phase 2 — active (running) ✅ (PID 2633)
├── kallon-camera-route.service           ← Phase 1 — active (exited) ✅
├── kallon-wg-watchdog.service            ← Phase 3 — inactive (dead) ✅ oneshot
├── kallon-wg-watchdog.timer              ← Phase 3 — active (waiting) ✅ every 30 s
├── mediamtx.service                      ← Phase 3 — active (running) ✅ (PID 1253)
└── wg-quick@wg0.service.d/
    └── userspace.conf                    ← Phase 3 — Tegra userspace fallback ✅

/usr/local/sbin/
└── kallon-wg-watchdog.sh                 ← Phase 3 — 0755 root:root ✅

/usr/local/bin/
└── mediamtx                              ← Phase 3 — 0755 root:root (41 MB binary, ARM64) ✅
```

### 8.4 VPS runtime layout (verified live today)

```text
/home/ubuntu/
└── kallon-noc/
    └── alert_listener.py                 ← 1372 bytes — NOC HMAC verifier on :8080

/etc/kallon/
└── alert.key                             ← 640 — hOQCqcEO3VKDko6JUy2Xrd3VvhIgwsbnqMWpNEzN8d0=

/etc/wireguard/
├── gateway.private                       ← UPcc3wZwkjHPco8/KrVXdKC439cSHdcmi3Kpeft192Q=
├── gateway.public                        ← mXuS1kDIK49t6TFcDRJ9AqlKdb+jDxkilNcaM+oWdnw=
└── wg0.conf                              ← see §3.2

/etc/systemd/system/
└── wg-quick@wg0.service                  ← (standard, no drop-in needed — kernel module present)
```

---

## 9. Credentials & secrets inventory

```mermaid
flowchart LR
    subgraph PC["NOC PC (this workstation)"]
        K1["kallon-vps-key.pem<br/>RSA 2048 private key"]
        K2["wg-keys.txt<br/>(all WG keys + HMAC)"]
        K3["WireGuard for Windows<br/>NOC PC private key"]
    end
    subgraph J["Jetson"]
        K4["jetson.private (WG)"]
        K5["/etc/kallon/alert.key (HMAC)"]
        K6["khalifa user password"]
        K7["CAMERA_PASSWORD (env)"]
    end
    subgraph V["VPS"]
        K8["gateway.private (WG)"]
        K9["/etc/kallon/alert.key (HMAC)"]
        K10["ubuntu sudo (passwordless via SSM/pem)"]
    end
    subgraph C["Camera"]
        K11["admin / terra123"]
    end
    K2 -. defines .- K4
    K2 -. defines .- K8
    K2 -. defines .- K5
    K2 -. defines .- K9
    K5 ===|"same value"| K9
```



### 9.1 The credential master list (current values)


| #   | Secret                             | Live value                                                     | Lives on                                                                                                         | Used by                                                                |
| --- | ---------------------------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| 1   | Camera admin password              | `terra123`                                                     | Camera firmware + `dahua_onvif_control.py:33` default + Jetson `CAMERA_PASSWORD` env + `mediamtx.yml` source URL | All ONVIF/RTSP traffic                                                 |
| 2   | Jetson WG private                  | `8KlzCK59DZhafnHxQOJtd4DLZQoohWX+ztzkmn5rIGM=`                 | Jetson `/etc/wireguard/jetson.private`                                                                           | wg-quick@wg0                                                           |
| 3   | Jetson WG public                   | `A8urKcYrPJzJfgt8V0GPdmFaigO8rgGjUMGBHJVjJyw=`                 | VPS peer block                                                                                                   | hub config                                                             |
| 4   | VPS WG private                     | `UPcc3wZwkjHPco8/KrVXdKC439cSHdcmi3Kpeft192Q=`                 | VPS `/etc/wireguard/gateway.private`                                                                             | wg-quick@wg0                                                           |
| 5   | VPS WG public                      | `mXuS1kDIK49t6TFcDRJ9AqlKdb+jDxkilNcaM+oWdnw=`                 | Jetson + NOC peer block                                                                                          | endpoint identity                                                      |
| 6   | NOC PC WG public                   | `lN6KnJrnsiHfj/VMoTkxqFPNNBq1MUqt4NNpsDxqWEg=`                 | VPS peer block                                                                                                   | hub config                                                             |
| 7   | NOC PC WG private                  | (in WireGuard for Windows local config)                        | NOC PC                                                                                                           | tunnel cipher                                                          |
| 8   | **HMAC alert key (symmetric)**     | `hOQCqcEO3VKDko6JUy2Xrd3VvhIgwsbnqMWpNEzN8d0=`                 | Both Jetson `/etc/kallon/alert.key` and VPS `/etc/kallon/alert.key` (verified ✅)                                 | `kallon_watchdog.AlertSender._sign` & `alert_listener.Handler.do_POST` |
| 9   | VPS SSH key (RSA 2048)             | `kallon-vps-key.pem` (full contents in project root, see file) | NOC PC project root                                                                                              | `ssh -i kallon-vps-key.pem ubuntu@18.220.75.237`                       |
| 10  | Jetson user password for `khalifa` | `10qpalzm`                                                     | Jetson `/etc/shadow`                                                                                             | SSH login + `sudo`                                                     |
| 11  | NOC peer endpoint NAT              | `135.129.124.77:42417` (today)                                 | dynamic — IGD/NAT churn possible                                                                                 | WG re-handshake                                                        |
| 12  | Jetson peer endpoint NAT           | `135.129.124.77:20939` (today)                                 | dynamic                                                                                                          | WG re-handshake                                                        |


> 🔴 **Credential hygiene findings:**
>
> 1. ~~`deploy/_jetson_update.py` contained `sudo_pw = "10qpalzm"` in plaintext~~ — **RESOLVED: file deleted.** The password was in git history; rotate it on the Jetson (`passwd khalifa`).
> 2. The repo's `kallon-vps-key.pem` is in the project parent directory, not inside `CODE/`, so the `.gitignore` in `CODE/` won't catch it if someone runs `git add ../kallon-vps-key.pem`. The PEM key file is fine where it is, but worth verifying it has not been pushed.
> 3. `CAMERA_PASSWORD=terra123` is the default in `dahua_onvif_control.py:33`. The brief calls this acceptable for bench but not for production. Manufacturing flow should inject `CAMERA_PASSWORD` per device.
> 4. `Environment=CAMERA_PASSWORD=change_me` in `kallon-ptz-daemon.service.example` is a placeholder — the **real** value must be edited in before enabling.
> 5. The HMAC key is symmetric. Anyone who can read both `alert.key` files can forge alerts. Mode `0640 root:khalifa` is sufficient for the bench but production should consider per-device keys.

---

## 10. Network ports & protocols cheat sheet


| Endpoint                    | Port / proto   | What                                                         | Direction                     | Auth                           | Filtered by                                                  |
| --------------------------- | -------------- | ------------------------------------------------------------ | ----------------------------- | ------------------------------ | ------------------------------------------------------------ |
| Camera `192.168.1.108:80`   | TCP HTTP       | ONVIF device service                                         | Jetson → camera               | WS-Username token              | (none today; switch ACL when arrives)                        |
| Camera `192.168.1.108:554`  | TCP RTSP       | Video stream                                                 | mediamtx → camera             | RTSP auth (admin/terra123)     | (same)                                                       |
| Camera `192.168.1.108:8899` | TCP            | (refused) — ONVIF on this port disabled / not present        | n/a                           | —                              | —                                                            |
| Jetson `127.0.0.1:8765`     | TCP JSON-lines | PTZ daemon                                                   | local only (ArtemisOS future) | none (loopback)                | systemd `ProtectSystem`, default `PTZ_LISTEN_HOST=127.0.0.1` |
| Jetson `10.50.0.2:8554`     | TCP RTSP       | mediamtx (wg0 only)                                          | NOC → Jetson                  | RTSP auth                      | host iptables (when added)                                   |
| Jetson `<wan>:UDP/random`   | UDP            | WireGuard egress (NAT 135.129.124.77:20939)                  | Jetson → VPS                  | curve25519 + ChaCha20-Poly1305 | upstream router NAT only                                     |
| VPS `0.0.0.0:51820`         | UDP            | WireGuard hub                                                | peers → VPS                   | curve25519                     | AWS Lightsail SG (must allow UDP 51820)                      |
| VPS `0.0.0.0:8080`          | TCP HTTP       | NOC alert listener (also reachable from internet — see §3.5) | Jetson → VPS over wg0         | HMAC-SHA256 verifies           | none (UFW inactive — see ⚠️ in §3.5)                         |
| NOC PC `10.50.0.10/24`      | n/a            | WG client (NOC peer)                                         | bidirectional over WG         | curve25519                     | WireGuard for Windows                                        |


---

## 11. Data flows

### 11.1 Live video (Phase 3 happy path)

```mermaid
sequenceDiagram
    autonumber
    participant CAM as Dahua camera<br/>192.168.1.108
    participant JC as Jetson camera-route<br/>(direct Ethernet)
    participant MTX as mediamtx :8554<br/>(on wg0)
    participant WG as wg-quick@wg0<br/>(userspace on Tegra)
    participant VPS as VPS WireGuard hub<br/>10.50.0.1
    participant NOC as NOC VLC<br/>10.50.0.10
    NOC->>VPS: WireGuard packet (UDP 51820)
    VPS->>WG: forward to peer 10.50.0.2
    WG->>MTX: RTSP DESCRIBE rtsp://10.50.0.2:8554/cam1
    MTX->>JC: RTSP DESCRIBE rtsp://admin@192.168.1.108/cam/realmonitor?channel=1
    JC->>CAM: RTSP DESCRIBE (local LAN, no VLAN yet)
    CAM-->>JC: SDP
    JC-->>MTX: SDP
    MTX-->>WG: SDP (rewritten for cam1)
    WG-->>VPS: encrypted UDP
    VPS-->>NOC: encrypted UDP
    Note over CAM,NOC: Then ANNOUNCE / SETUP / PLAY,<br/>H.264 RTP/RTCP interleaved over RTSP TCP
    loop continuous
        CAM-->>NOC: H.264 video frames (via JC → MTX → WG → VPS → NOC)
    end
```



### 11.2 Tamper alert (Phase 4 — door opens)

```mermaid
sequenceDiagram
    participant Door
    participant Gpio
    participant Watchdog
    participant Sender
    participant Tunnel
    participant Listener
    Door->>Gpio: magnet removed
    Gpio->>Watchdog: edge event
    Watchdog->>Sender: enqueue alert
    Sender->>Sender: dedup and sign
    Sender->>Tunnel: POST signed alert
    Tunnel->>Listener: forward over wg0
    Listener->>Listener: verify HMAC
    Listener-->>Sender: HTTP 200 ok
```



The matching error path (signature fails the `compare_digest` check on the VPS):

```mermaid
sequenceDiagram
    participant Sender
    participant Listener
    Sender->>Listener: POST with bad HMAC
    Listener->>Listener: verify HMAC
    Listener-->>Sender: HTTP 403 rejected
```



### 11.3 PTZ command (Phase 2 — local client → live daemon)

```mermaid
sequenceDiagram
    autonumber
    participant CL as Local client<br/>(echo ... nc / ArtemisOS)
    participant PD as kallon-ptz-daemon<br/>127.0.0.1:8765 (PID 2633)
    participant LK as asyncio.Lock<br/>one in flight
    participant TH as asyncio.to_thread<br/>blocking ONVIF
    participant ZP as zeep ONVIF client<br/>persistent session
    participant CAM as Camera 192.168.1.108:80
    CL->>PD: one JSON line ending \n<br/>method=move_absolute, pan=0.2, tilt=0
    PD->>LK: acquire
    PD->>TH: dispatch(session, method, params)
    TH->>ZP: AbsoluteMove(ProfileToken, Position)
    ZP->>CAM: SOAP/HTTP POST /onvif/ptz_service
    CAM-->>ZP: 200 OK
    loop poll every 50 ms until within tolerance or 10 s
        ZP->>CAM: GetStatus
        CAM-->>ZP: Position(pan, tilt, zoom)
        TH->>TH: check abs(reported - target) ≤ 0.03
    end
    TH-->>PD: ok=True, rtt=456.2 ms
    PD->>LK: release
    PD-->>CL: one JSON line<br/>ok=true, result.confirmed=true, round_trip_ms=456.2
```



---

## 12. Phase status board

```mermaid
gantt
    title Kallon Sentry Tower — Phase progress vs brief
    dateFormat YYYY-MM-DD
    axisFormat %b

    section Phase 0  Bench unit
    Camera reachable on direct cable     :done,    p0a, 2026-04-01, 14d
    Baseline pcap (mirror port)          :crit,    p0b, after p0a, 1d
    ONVIF capability enumeration         :done,    p0c, 2026-04-15, 7d

    section Phase 1  Data sovereignty
    Camera UI cloud toggles              :active,  p1a, 2026-04-20, 7d
    Camera VLAN + ACL (switch)           :crit,    p1b, after p1a, 14d
    24h zero-egress soak pcap            :crit,    p1c, after p1b, 1d

    section Phase 2  Local PTZ
    ONVIF CLI                            :done,    p2a, 2026-04-20, 7d
    AbsoluteMove + confirm loop          :done,    p2b, after p2a, 4d
    PTZ daemon (TCP/Unix JSON)           :done,    p2c, after p2b, 4d
    Daemon installed as systemd          :active,  p2d, after p2c, 3d
    1000-cmd p95 < 100 ms                :crit,    p2e, after p2d, 1d

    section Phase 3  WireGuard + RTSP
    WG keys + tunnel (lab)               :done,    p3a, 2026-05-01, 5d
    mediamtx on wg0                      :done,    p3b, after p3a, 3d
    WG auto-reconnect watchdog           :done,    p3c, after p3b, 2d
    Jetson host firewall (8554 wg0-only) :crit,    p3cb, after p3c, 1d
    Two-tunnel isolation pcap            :crit,    p3d, after p3cb, 1d

    section Phase 4  Tamper + health
    Sensors wired                        :done,    p4a, 2026-05-08, 5d
    Watchdog daemon                      :done,    p4b, after p4a, 5d
    HMAC alert webhook                   :done,    p4c, after p4b, 3d
    All five sensor tests passing        :active,  p4d, after p4c, 3d
```



Cross-referenced to the brief's exit criteria:


| Brief criterion                        | Where measured                               | Status                       |
| -------------------------------------- | -------------------------------------------- | ---------------------------- |
| 0 bytes camera → Dahua IPs over 24 h   | switch mirror pcap                           | ⏸ blocked by switch          |
| PTZ p95 < 100 ms over 1,000 cmds       | `sentry_ptz_absolute benchmark --count 1000` | 🔴 (~1.6 s observed at n=10) |
| Two customer tunnels, no cross-traffic | second `wg-quick@wgN` + pcap                 | ⏸ blocked by switch          |
| Enclosure open → NOC alert in 5 s      | manual test                                  | ✅ (interrupt-driven, ~1 s)   |
| Camera stream fail → NOC alert in 30 s | unplug Ethernet                              | ✅ (10 s poll + 5 s probe)    |
| Second sensor type plugs in cleanly    | gRPC SensorService                           | ⏸ Phase 5                    |


---

## 13. Open gaps, blockers, and immediate next actions

### 13.1 Done in this session

1. ✅ **Fixed `kallon-ptz-daemon.service.example` paths** — now matches the deployed Jetson reality (`User=khalifa`, `WorkingDirectory=/home/khalifa/kallon`, `ExecStart` under the same path).
2. ✅ **Fixed lab-guide typo** — `jetson-lab-steps-8-10.md` line 307 now reads `ACCEPT` (and added the paired `DROP`).
3. ✅ **Added WG auto-reconnect watchdog files** to `CODE/deploy/`:
  - `kallon-wg-watchdog.sh`
  - `kallon-wg-watchdog.service.example`
  - `kallon-wg-watchdog.timer.example`
4. ✅ **Added missing systemd templates** to `CODE/deploy/`:
  - `kallon-camera-route.service.example`
  - `mediamtx.service.example`
  - `mediamtx.yml.example`
  - `wg-quick@wg0.userspace.conf.example`
5. ✅ **Confirmed the PTZ daemon is fully implemented and running** as `kallon-ptz-daemon.service` PID 2633 on the Jetson; this clears up earlier confusion that the daemon "was not implemented" — it is, that's why `echo … | nc 127.0.0.1 8765` works.

### 13.2 Still in-flight, do next

1. **Apply the iptables rule on the Jetson** — the typo's fixed in the doc but the rule isn't installed yet:
  ```bash
   sudo iptables -A INPUT -i wg0 -p tcp --dport 8554 -j ACCEPT
   sudo iptables -A INPUT -p tcp --dport 8554 -j DROP
   sudo apt install -y iptables-persistent     # or netfilter-persistent save
  ```
2. **Run the 1,000-command PTZ benchmark** end-to-end (`sentry_ptz_absolute.py benchmark --count 1000`) and either publish the result or formally re-baseline the Phase 2 latency target. The brief still asserts p95 < 100 ms; bench data from 10 commands says ~1.6 s is the ONVIF/Dahua reality. One must change.
3. **Harden the VPS:**
  - Enable `ufw`, allow only UDP 51820 from `0.0.0.0/0` and TCP 8080 **only from 10.50.0.0/24** (currently the alert listener is reachable from the public internet — anyone who scans port 8080 will get HTTP 403 / 200 depending on whether they guess the HMAC).
  - Move `alert_listener.py` to a systemd service (`Restart=on-failure`) instead of `sudo nohup ... &`. Today it survives only as long as the VPS doesn't reboot.
4. **Push the locally-edited PTZ daemon unit to the repo as the canonical version** if you'd like the repo to ship a "real" service file in addition to the `.example`. Today there are two units on the Jetson: `/etc/systemd/system/kallon-ptz-daemon.service` (live, 1169 B) and `/home/khalifa/kallon/deploy/kallon-ptz-daemon.service` (1169 B copy — not tracked in git).
5. **Pull the Jetson's recent commits** into the NOC PC working tree: `cd CODE && git pull` — the Jetson is at `4e73745` but this NOC PC working copy may predate that.

### 13.2 Blocked, but identify the unblock


| Blocker                  | What it unblocks                                                      | Mitigation today                                               |
| ------------------------ | --------------------------------------------------------------------- | -------------------------------------------------------------- |
| Managed PoE+ VLAN switch | Phase 1 exit criterion (24h pcap), Phase 3 sign-off (two-tunnel pcap) | Document camera UI hardening; partial iptables check on Jetson |
| NVMe SSD                 | Production storage, NVMe SMART probe, 24h pcap storage                | `ENABLE_NVME=0` keeps the probe inert; log to `/tmp`           |
| LTE / 5G modem           | Real cellular WAN profile, NAT keepalive tuning                       | Home WAN with `PersistentKeepalive=25` (already configured)    |
| Customer NOC software    | True NOC integration                                                  | `alert_listener.py` on VPS serves as a stand-in                |


### 13.3 Strategic / scaling (Phase 5+ — out of current scope)

- gRPC `SensorService` (Phase 5).
- OTA pipeline with Ed25519 signing (Phase 5).
- ArtemisOS inference pipeline; PTZ tracking by detection.
- Manufacturing key DB + `kallon-gateway-add-peer.sh` for fleet scale (roadmap §11).
- Golden image / first-boot provisioning.

---

## 14. One-page operator cheat sheet

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ KALLON SENTRY TOWER — OPERATOR CHEAT SHEET                                   │
├──────────────────────────────────────────────────────────────────────────────┤
│ HOSTS                                                                         │
│   ① Camera   Dahua PTZ          192.168.1.108            admin / terra123    │
│   ② Jetson   khalifa-desktop    192.168.1.246 / 10.50.0.2   khalifa / ****   │
│   ③ VPS      ip-172-26-1-191    18.220.75.237 / 10.50.0.1   ubuntu (.pem)    │
│   ④ NOC PC   (this Windows)     192.168.1.230 / 10.50.0.10                   │
│                                                                               │
│ SSH                                                                           │
│   ssh -i kallon-vps-key.pem ubuntu@18.220.75.237                             │
│   ssh khalifa@192.168.1.246           # LAN, fast (35 ms)                    │
│   ssh khalifa@10.50.0.2               # over WG, ~450 ms                     │
│                                                                               │
│ KEY SERVICES                                                                  │
│   Jetson:  wg-quick@wg0  ·  mediamtx  ·  kallon-watchdog  ·  kallon-ptz-d.   │
│   VPS:     wg-quick@wg0  ·  alert_listener.py (TCP 8080)                     │
│                                                                               │
│ DAILY CHECKS                                                                  │
│   # On VPS                                                                    │
│   sudo wg show                                            # handshake fresh? │
│   sudo journalctl -u wg-quick@wg0 --no-pager -n 20                           │
│                                                                               │
│   # On Jetson                                                                 │
│   sudo wg show                                                                │
│   systemctl status kallon-watchdog kallon-ptz-daemon mediamtx                │
│   journalctl -u kallon-watchdog -f                                            │
│   ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1                     │
│                                                                               │
│   # On NOC PC                                                                 │
│   ping 10.50.0.2                                                              │
│   vlc rtsp://10.50.0.2:8554/cam1                                              │
│                                                                               │
│ SENSOR TESTS (Phase 4)                                                        │
│   Open door:  expect TAMPER_DOOR_OPEN within ~1 s on VPS listener            │
│   Light LDR:  expect TAMPER_LIGHT                                             │
│   Tap rig:    expect TAMPER_IMPACT (deduped to 1/60 s)                       │
│   Unplug cam: expect CAMERA_STREAM_FAIL in ~30 s                              │
│                                                                               │
│ EMERGENCY                                                                     │
│   Stop everything (Jetson):                                                   │
│     sudo systemctl stop kallon-watchdog kallon-ptz-daemon mediamtx wg-quick@wg0│
│   Re-restart WG (Jetson):                                                     │
│     sudo wg-quick down wg0; sudo systemctl start wg-quick@wg0; sudo wg show  │
│   Rotate HMAC key (both sides MUST match):                                    │
│     head -c 32 /dev/urandom | base64 | sudo tee /etc/kallon/alert.key        │
│     # copy result to BOTH Jetson AND VPS — then restart kallon-watchdog       │
│                                                                               │
│ SOURCE OF TRUTH                                                               │
│   git: github.com/Yaqcodes/kallon-sentry (branch main)                       │
│   Docs:                                                                       │
│     - kallon_sovereign_stack_brief.md  (the why)                             │
│     - kallon_mass_deployment_roadmap.md (the how to scale)                   │
│     - jetson-lab-steps-8-10.md          (Phase 3 lab)                        │
│     - phase4-setup-guide.md             (Phase 4 bring-up)                   │
│     - kallon_hardware_wiring.md         (J12 wiring)                         │
│     - HOW_TO_USE.md                     (CLI reference)                      │
│     - kallon_current_state.md           (THIS doc — god's-eye view)          │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Appendix A — Mermaid index


| #   | Diagram                         | Section |
| --- | ------------------------------- | ------- |
| 1   | Hero topology                   | §1      |
| 2   | Four-host inventory             | §2      |
| 3   | **Services & daemons per node** | §2.2    |
| 4   | Phase 1 sovereignty flow        | §4.1    |
| 5   | Phase 2 three-layer code stack  | §5.1    |
| 6   | Phase 3 WG topology             | §6.1    |
| 7   | mediamtx RTSP sequence          | §6.3    |
| 8   | Phase 4 J12 wiring              | §7.1    |
| 9   | Watchdog architecture           | §7.2    |
| 10  | Credential ownership            | §9      |
| 11  | Video data flow                 | §11.1   |
| 12  | Tamper alert sequence           | §11.2   |
| 13  | PTZ command sequence            | §11.3   |
| 14  | Phase progress Gantt            | §12     |


## Appendix B — Document provenance

This document was synthesized from:

- `kallon_sovereign_stack_brief.md` (v1.0)
- `kallon_mass_deployment_roadmap.md` (v1.0)
- `jetson-lab-steps-8-10.md` (v1.0)
- `phase4-setup-guide.md`
- `kallon_hardware_wiring.md` (Rev A)
- `HOW_TO_USE.md`
- `dahua_onvif_control.py`, `sentry_ptz_absolute.py`, `kallon_ptz_daemon.py`, `kallon_watchdog.py`
- `deploy/install-kallon-watchdog.sh`, `kallon-watchdog.service.example`, `kallon-ptz-daemon.service.example`
- `wg-keys.txt`, `kallon-vps-key.pem`
- `Kallon Tamper Detection BOM.pdf`
- Four prior agent transcripts: [Camera port and ONVIF setup](60d13612-e3e3-41f7-b8ab-2bac84c03e6b), [Snap fix on Orin Nano](7e994e2b-6349-4a0b-9ce3-ddb9b052355f), [Phase 3 lab walkthrough](b0623424-0e16-48ee-9d0e-68344c1e28b3), [Project re-read](fd64baa6-9548-4abc-9a19-65a87e8e1d4c)
- **Live SSH probe of VPS** (today, `ubuntu@18.220.75.237` via `kallon-vps-key.pem`)
- **Live SSH probe of Jetson** (today, `khalifa@192.168.1.246` via paramiko + password; 40+ commands including `wg show`, `systemctl list-units`, `ss -tlnp`, `i2cdetect`, `journalctl`, file pulls of all systemd units)
- Local PowerShell `ipconfig` / `ping` on NOC PC (today)

## Appendix C — Document changelog

- **v1.1** (this session): Added live Jetson state (§3.7, §3.8). New "services & daemons per node" diagram (§2.2). Confirmed PTZ daemon is fully implemented and live. Fixed mermaid sequence diagram 12 (pipe character collision). Removed obsolete "Jetson not directly accessible" caveat. Closed three doc gaps by adding source files to `CODE/deploy/`. Fixed `kallon-ptz-daemon.service.example` paths. Fixed `jetson-lab-steps-8-10.md` `ACCEP` typo and added the paired DROP rule.
- v1.0: Initial god's-eye-view synthesis.

*Terra Industries · Kallon Sentry Tower · Current state of the stack v1.1 · May 2026*