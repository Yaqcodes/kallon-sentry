# Kallon Sentry Tower — Path to Mass Deployment

**Terra Industries · Internal · May 2026**

This document turns the **lab bench** (home LAN + Lightsail gateway) into a **repeatable production model**: factory provision → field install → customer-owned gateway and NOC.

**Related docs**

| Document | Role |
|----------|------|
| `kallon_sovereign_stack_brief.md` | Full phased product plan and exit criteria |
| `jetson-lab-steps-8-10.md` | Bench walkthrough (WireGuard, mediamtx, step 9 scripts) |
| `HOW_TO_USE.md` | ONVIF CLI and PTZ daemon usage |

---

## 1. Executive summary

**Proven in lab (you):**

- Dahua camera on direct Ethernet to Jetson (ONVIF / RTSP)
- `mediamtx` rebroadcast on `:8554`
- WireGuard to customer gateway (Lightsail), userspace WG on tegra
- Customer-style viewing: PC on VPN → `rtsp://10.50.0.2:8554/cam1` (TCP)
- Persisted pieces: `wg-quick@wg0`, mediamtx, camera route systemd unit, iptables for RTSP

**Not yet productized:**

- One-shot installer, manufacturing key flow, health/alert daemon, gateway automation, managed switch / LTE profile, tamper hardware, ArtemisOS, OTA

**Next engineering milestone:** **`deploy/` templates + `scripts/kallon-jetson-install.sh`** so a fresh Jetson matches a golden bench in one run.

---

## 2. Production architecture (target)

```text
┌─────────────┐   PoE/L2    ┌──────────────────────────────────────┐
│ Dahua cams  │ ──────────► │ Jetson Orin NX                        │
│ (ONVIF only)│   VLAN      │  • camera route (Ethernet)            │
└─────────────┘             │  • ONVIF / PTZ daemon                  │
                            │  • mediamtx :8554 on wg0               │
                            │  • WireGuard wg0 (userspace on L4T)    │
                            │  • health + WG watchdogs              │
                            └──────────────┬───────────────────────┘
                                           │ LTE / WAN (no camera internet)
                                           ▼
                            ┌──────────────────────────────────────┐
                            │ Customer VPN gateway (self-hosted)    │
                            │  • UDP 51820, ip_forward              │
                            │  • peers: each tower + optional NOC   │
                            └──────────────┬───────────────────────┘
                                           │
                    ┌──────────────────────┴──────────────────────┐
                    ▼                                              ▼
            RTSP (VMS/VLC)                              HTTPS webhook (alerts)
            rtsp://10.50.0.x:8554/cam1                  POST + HMAC-SHA256
```

**Principles**

- Customer owns gateway, keys, and data path — Terra not in the media path post-provision.
- Per-device WireGuard keypair; `AllowedIPs` scoped to customer VPN prefix (not `0.0.0.0/0` on the tower).
- Video API = **RTSP over VPN**; alerts API = **signed HTTP POST** (spec in sovereign brief §Phase 4).

---

## 3. What the codebase is today

| Component | In repo | Production-ready |
|-----------|---------|------------------|
| `dahua_onvif_control.py` | Yes | Bench / ops tool |
| `kallon_ptz_daemon.py` | Yes | Needs packaged service + benchmark |
| `deploy/kallon-ptz-daemon.service.example` | Yes | Example only |
| WireGuard / mediamtx / camera route | Documented + manual | **Not automated in repo** |
| `kallon-wg-provision` | Spec in lab §9.1 | **Not in repo** |
| `kallon-wg-watchdog` | Spec in lab §9.2 | **Not in repo** |
| Health + alert webhook | Spec in brief §4 | **Not in repo** |
| Customer gateway playbook | Partial (lab steps) | **Needs `docs/customer-gateway.md`** |
| Managed switch / zero egress | Brief §Phase 1 | Hardware blocked |
| Tamper / GPIO | Brief §Phase 4 | Not started |
| ArtemisOS gRPC / OTA | Brief §Phase 5 | Not started |

**Verdict:** Architecture validated; **packaging and operations layer** are the gap.

---

## 4. Customer-facing interfaces (“APIs”)

There is no single REST portal in v1. Customers integrate via:

### 4.1 Video — RTSP over VPN

| Item | Value |
|------|--------|
| URL pattern | `rtsp://<tower-vpn-ip>:8554/<path>` (lab: `10.50.0.2`, path `cam1`) |
| Transport | **TCP** (required over VPN / NAT) |
| Source | `mediamtx` pulling camera RTSP locally |

**Customer action:** Open in VMS (Genetec, Milestone, VLC, ffprobe). Document firewall: TCP 8554 tower-side already on `wg0`; gateway forwards between peers.

### 4.2 Alerts — signed webhook (to implement)

| Item | Value |
|------|--------|
| Method | `POST` to `ALERT_WEBHOOK_URL` (reachable over VPN, e.g. `http://10.50.0.1:8080/alerts`) |
| Header | `X-Kallon-Signature: <hmac-sha256-hex>` |
| Body | JSON: `device_id`, `timestamp_utc`, `alert_type`, `severity`, `details` |
| Secret | `/etc/kallon/alert.key` (mode `600`, device-only) |

**Customer action:** Implement verifier (brief §Phase 4). Terra provides reference nginx/Python snippet in `docs/alert-webhook.md` (TODO).

### 4.3 PTZ — local control plane (optional)

| Item | Value |
|------|--------|
| Interface | TCP JSON to `kallon_ptz_daemon` (see `HOW_TO_USE.md`) |
| Consumers | ArtemisOS / local automation — not exposed to public internet |

### 4.4 Future (Phase 5)

- gRPC `SensorService` for ArtemisOS
- OTA agent (separate WireGuard peer or channel — not customer media tunnel)

---

## 5. Manufacturing and provisioning model

### 5.1 Per-unit identity

| Field | Example | Stored |
|-------|---------|--------|
| `DEVICE_ID` | `kallon-001` | `/etc/kallon/device.env` |
| Jetson WG private key | — | `/etc/wireguard/jetson.private` (never git) |
| Jetson WG public key | base64 | Manufacturing DB + gateway peer |
| VPN address | `10.50.0.2/24` | `device.env` + `wg0.conf` |
| Gateway endpoint | `vpn.customer.com:51820` | `device.env` |
| Gateway public key | base64 | `device.env` → `[Peer] PublicKey` |
| Camera password | — | `device.env` or inject at install (mode `600`) |
| Alert HMAC key | random | `/etc/kallon/alert.key` |

### 5.2 Factory / staging flow

```text
1. Flash golden Jetson image (Ubuntu L4T 22.04 + base packages)
2. Attach label: DEVICE_ID + QR
3. Run: kallon-wg-provision (generates keys if absent, renders wg0.conf)
4. Register jetson.public in customer gateway (or staging gateway)
5. Run: kallon-jetson-install.sh --env /etc/kallon/device.env
6. Acceptance: handshake, ffprobe 127.0.0.1:8554/cam1, ping gateway
7. Ship with device.env on secure USB or pre-baked in image partition
```

### 5.3 Field install (installer tech)

- Mount tower, connect cameras to PoE switch (production) or direct Ethernet (pilot).
- LTE modem or WAN as designed; **no** camera default route to internet.
- Power on → services start → VPN handshake within 2 min.
- Customer NOC confirms RTSP + test alert.

### 5.4 Gateway (per customer)

- One gateway per customer deployment (VPS or on-prem).
- **UDP 51820** open on cloud firewall.
- `net.ipv4.ip_forward=1`; peers for each tower + optional NOC (`10.50.0.10/32`).
- Peer `AllowedIPs` = tower `/32` only (lab used `/32` for Jetson — correct).

---

## 6. Jetson software stack (install target)

After `kallon-jetson-install.sh`, a unit should have:

| Piece | Mechanism |
|-------|-----------|
| Packages | `wireguard-tools`, `wireguard-go`, `ffmpeg`, `python3-pip`, `iptables-persistent` |
| App | `/opt/kallon` from git tag, `pip3 install -r requirements.txt` |
| mediamtx | Pinned arm64 binary + `/etc/mediamtx.yml` |
| VPN | `/etc/wireguard/wg0.conf`, `wg-quick@wg0` + **userspace drop-in** (`WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/bin/wireguard`) |
| Camera L3 | `kallon-camera-route.service` (or netplan where applicable) |
| RTSP firewall | iptables: `lo` + `wg0` ACCEPT 8554, DROP other |
| WG watchdog | `kallon-wg-watchdog.timer` (restart if handshake > 60s) |
| Health watchdog | `kallon-health-watchdog.service` (RTSP + temp → webhook) |
| PTZ (optional) | `kallon-ptz-daemon.service` |

**L4T note:** Jammy Jetson ships `wireguard-go` binary as `/usr/bin/wireguard` — installer must set userspace env (see `jetson-lab-steps-8-10.md`).

**Camera route note:** Many Jetson images have **no `/etc/netplan/`** — use **systemd oneshot** for `192.168.1.108/32 → enP8p1s0` (not netplan-only).

---

## 7. Repo deliverables checklist

Copy this into issues/PRs; check off as shipped.

### 7.1 `deploy/` (templates)

- [ ] `device.env.example`
- [ ] `wg0.conf.example`
- [ ] `wg-quick@wg0.service.d/userspace.conf`
- [ ] `kallon-camera-route.service`
- [ ] `kallon-wg-watchdog.sh`, `.service`, `.timer`
- [ ] `kallon-health-watchdog.sh`, `.service`
- [ ] `mediamtx.yml.example`, `mediamtx.service`
- [ ] `iptables-rebroadcast.rules.example`
- [ ] `kallon-ptz-daemon.service` (from existing example)

### 7.2 `scripts/`

- [ ] `kallon-jetson-install.sh` — idempotent install/enable
- [ ] `kallon-wg-provision.sh` — keys + render `wg0.conf`
- [ ] `kallon-gateway-add-peer.sh` — run on gateway to add tower peer
- [ ] `kallon-acceptance.sh` — ping, wg, ffprobe, webhook dry-run

### 7.3 `docs/`

- [ ] `customer-gateway.md` — Lightsail/AWS/on-prem, peers, forwarding, NOC PC
- [ ] `alert-webhook.md` — schema + HMAC verification examples
- [ ] `manufacturing-runbook.md` — factory steps, label, key registry

### 7.4 Tests / sign-off scripts

- [ ] `scripts/signoff-phase3.sh` — VPN RTSP from gateway host
- [ ] Phase 1 packet capture procedure (when switch arrives)

---

## 8. Phased roadmap

| Phase | Goal | Mass-deploy blocker? | Est. |
|-------|------|----------------------|------|
| **Lab (done)** | VPN video path | — | Done |
| **P0 — Package bench** | Installer + `deploy/*` + acceptance script | **Yes** | 1–2 weeks |
| **P1 — Network hardening** | Managed switch, camera VLAN, 24h zero-egress capture | Yes for sovereign sign-off | 2 weeks + hardware |
| **P2 — PTZ product** | Daemon hardened, 1000-cmd benchmark | No for video-only deploy | 2 weeks |
| **P3 — Alerts** | Health watchdog + customer webhook verifier | Yes for monitoring SLA | 1–2 weeks |
| **P4 — Field WAN** | LTE profile, NAT keepalive tuning | Yes for tower deployment | 2 weeks |
| **P5 — Tamper** | GPIO/I2C + alert types | No for pilot video | 2 weeks |
| **P6 — Scale** | Golden image, key DB, gateway IaC | Yes for mass production | 3–4 weeks |
| **P7 — Platform** | ArtemisOS, OTA | Future | Ongoing |

**Minimum viable production (pilot towers):** P0 + P3 + customer gateway doc + field LTE (P4).

**Full sovereign sign-off:** P1 + P2 + P3 + two-tunnel isolation proof (brief §Phase 3).

---

## 9. Installer script specification (`kallon-jetson-install.sh`)

**Behavior**

1. Require root; refuse on non-arm64 without flag.
2. Read `/etc/kallon/device.env` (or `--env FILE`); validate required vars.
3. `apt install` packages; install mediamtx if missing (pin version).
4. Sync `/opt/kallon` from git tag or embedded tarball.
5. `pip3 install -r requirements.txt`.
6. Install systemd units from `deploy/`; `daemon-reload`.
7. Render `/etc/mediamtx.yml` from template (camera URL, password from env).
8. Run `kallon-wg-provision` if `wg0.conf` missing.
9. Install iptables rules (with `lo` ACCEPT documented).
10. Enable: `wg-quick@wg0`, `mediamtx`, `kallon-camera-route`, watchdog timers.
11. Run `kallon-acceptance.sh`; exit non-zero on failure.

**Idempotent:** Safe to re-run; does not rotate keys unless `--regenerate-keys`.

**Secrets:** Never log passwords; never commit `device.env` or `*.private`.

---

## 10. Acceptance tests (repeatable)

### 10.1 On Jetson (post-install)

```bash
ip route get 192.168.1.108          # → dev enP8p1s0
ping -c 2 10.50.0.1                # gateway over wg0
sudo wg show wg0 | grep handshake
timeout 45 ffprobe -rtsp_transport tcp -stimeout 10000000 \
  -i rtsp://127.0.0.1:8554/cam1
systemctl is-active wg-quick@wg0 mediamtx kallon-camera-route
```

### 10.2 On customer gateway

```bash
ping -c 2 10.50.0.2
timeout 45 ffprobe -rtsp_transport tcp -timeout 10000000 \
  -show_streams -i rtsp://10.50.0.2:8554/cam1
```

### 10.3 On NOC PC (WireGuard client)

```bash
ping 10.50.0.2
# VLC: rtsp://10.50.0.2:8554/cam1  (RTSP over TCP, MTU 1420 on tunnel)
```

### 10.4 Watchdog test

- Gateway: `sudo wg-quick down wg0`
- Jetson: within ~90s, `journalctl -u kallon-wg-watchdog` shows restart
- Gateway: `sudo wg-quick up wg0` → handshake and ping return

### 10.5 Alert test (when implemented)

- Stop mediamtx or unplug camera → webhook receives `CAMERA_STREAM_FAIL` within 30s; HMAC verifies.

---

## 11. Pilot vs mass production

| Aspect | Pilot (1–5 towers) | Mass production |
|--------|-------------------|-----------------|
| Imaging | Installer script on stock L4T | Golden image + first-boot |
| Keys | Manual peer on gateway | Manufacturing DB + `gateway-add-peer` API/script |
| Gateway | One Lightsail per customer | Terraform module per customer |
| Docs | Lab + this roadmap | Runbooks + customer PDFs |
| QA | `kallon-acceptance.sh` | Automated + burn-in rack |

---

## 12. Risks before scaling

| Risk | Mitigation |
|------|------------|
| Tegra WG kernel missing | Always userspace + systemd drop-in in installer |
| Camera routed via Wi‑Fi | `kallon-camera-route.service` in every install |
| RTSP blocked by iptables | `lo` + `wg0` rules in template |
| Customer PC not on VPN | Document NOC WireGuard peer; RTSP not on public internet |
| `ffprobe`/ffmpeg version flags | Document `-stimeout` (Jetson) vs `-timeout` (Ubuntu 24 gateway) |
| Secrets in git | `device.env.example` only; pre-commit check |

---

## 13. Immediate next actions (recommended order)

1. Add **`deploy/`** and **`scripts/kallon-jetson-install.sh`** to repo (P0).
2. Commit **`kallon-wg-watchdog`** + enable in installer.
3. Write **`docs/customer-gateway.md`** and **`docs/alert-webhook.md`**.
4. Implement **health watchdog** + test webhook on gateway (P3).
5. Order **managed switch**; run Phase 1 capture (P1).
6. Plan **LTE** interface profile (P4).
7. Second bench unit: prove installer on clean SD card.

---

## 14. Definition of “ready for deployment”

**Pilot-ready** when:

- [ ] Installer reproduces your bench in &lt; 1 hour on clean Jetson
- [ ] Customer gateway doc published; one customer peer tested
- [ ] VPS/NOC `ffprobe` over VPN passes
- [ ] Watchdog + persistence survive reboot
- [ ] Alert webhook e2e (at least `CAMERA_STREAM_FAIL`)

**Mass-ready** when additionally:

- [ ] Golden image or unattended first-boot
- [ ] Per-device key registry and gateway automation
- [ ] Phase 1 zero-egress proof with managed switch
- [ ] LTE field profile validated
- [ ] Manufacturing runbook and acceptance on production line

---

*Terra Industries · Kallon Sentry Tower · Mass deployment roadmap v1.0*
