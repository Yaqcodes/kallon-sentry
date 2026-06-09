# Kallon Sentry Tower — Sovereign Stack Project Brief

**Terra Industries · Confidential**
Version 2.0 · June 2026

| Related doc | Role |
|-------------|------|
| **`docs/field-test-setup.md`** | End-to-end setup & test walkthrough (field-test branch) |
| **`docs/postgres-windows-server-setup.md`** | Production control plane on Windows Server |
| `kallon_mass_deployment_roadmap.md` | Manufacturing, deployment, control plane, installer |
| `kallon_current_state.md` | Live bench verification (Phases 0–4) |
| `Considering physical server for VPS.md` | Control plane and hub hosting options |

---

## 1. Executive Summary

The Kallon sentry tower currently operates on a commercially sourced NVR stack that introduces critical architectural vulnerabilities: third-party cloud video routing, unreliable PTZ control dependent on manufacturer relays, no per-customer network isolation, and no hardware-level tamper or health monitoring. This brief documents the redesign of the internal electronics and software stack to produce a sovereign, edge-native surveillance node — eliminating Dahua and third-party cloud paths from the camera, and establishing the foundation for ArtemisOS integration and future sensor expansion.

**Buyer experience:** Customers buy Kallon towers, power them on, and monitor via the **Terra dashboard**. They do not configure VPNs, cloud consoles, or terminals. Terra operates the platform (registry, enrollment, customer hubs) behind the scenes.

**Deployment and manufacturing detail** — installer modules, registry, hub provisioner, identity formats — lives in `kallon_mass_deployment_roadmap.md`. This brief defines product intent and technical exit criteria; the roadmap defines how we build and ship.

The redesign does not affect the tower's physical shell, solar power system, or mounting hardware. The upgrade is self-contained to compute, networking, and sensor electronics inside the enclosure.

---

## 2. Problem Statement

### 2.1 Current Architecture

The production Kallon unit consists of:

- **Cameras:** IP PTZ cameras (ONVIF-compatible; Dahua in current stock)
- **Recorder:** Commercial NVR with spinning HDD
- **Power electronics:** Solar charge controller and battery bank
- **Networking:** No defined architecture; cameras connect directly to NVR

### 2.2 Identified Deficiencies

| # | Issue | Severity | Impact |
|---|-------|----------|--------|
| 1 | Camera video routed through Dahua cloud servers | Critical | Customer data transits third-party infrastructure; Terra has no control |
| 2 | PTZ control relies on manufacturer cloud relay | High | Commands are unreliable, high-latency, and fail when internet is unavailable |
| 3 | No per-customer network isolation | Critical | No cryptographic separation between customer data streams |
| 4 | No tamper detection or component health monitoring | High | Physical attacks and hardware failures are undetected until manual inspection |
| 5 | No sensor abstraction layer | Medium | Adding new sensors (radar, LiDAR, microphone) requires core system changes |

### 2.3 Root Cause

Camera firmware runs multiple parallel access stacks by default: ONVIF (local, standards-based), P2P cloud relay, DMSS cloud push, and proprietary SDK layers. The NVR has been configured to use the cloud path rather than local ONVIF/RTSP. The remediation path preserves ONVIF and eliminates all cloud egress from the camera subnet.

---

## 3. Goals and Success Criteria

### 3.1 Primary Goals

1. **Camera sovereignty:** Zero outbound camera traffic to Dahua or third-party cloud IPs; video is pulled locally by the Jetson over RTSP/ONVIF only.
2. **Turn-key buyer experience:** Power on → auto-enroll → live view and alerts in the Terra dashboard; no customer-operated infrastructure.
3. **Per-customer isolation:** Each customer org has a dedicated WireGuard hub; towers are cryptographically separated by VPN subnet and peer policy.
4. **Local PTZ:** ONVIF PTZ on the camera LAN with confirmation loop; no manufacturer cloud relay.
5. **Tamper and health:** Signed real-time alerts (door, impact, stream fail, temperature, etc.) within defined SLAs.
6. **Extensibility:** New sensors integrate via a plugin model without rewriting core tower firmware.

**Data path (retail):** Live video and alerts flow over WireGuard to a **Terra-provisioned hub per customer org**, then into the Terra dashboard. This replaces Dahua cloud routing; it is not anonymous third-party video hosting. Enterprise deployments may use a customer-hosted hub (Option C) under contract.

**Out of scope for v1:** Historical video playback, DVR, and long-term archive. v1 is **live monitoring + event alerts** only.

### 3.2 Success Criteria (Exit Conditions)

| Criterion | Measurement | Notes |
|-----------|-------------|-------|
| Zero outbound camera traffic to third-party IPs | 24h Wireshark on switch mirror port | Proven **once** on pilot build; assumed for identical production configs |
| PTZ command latency | p95 over 1,000 `AbsoluteMove` commands | Target p95 &lt; 100 ms; **re-baseline if ONVIF/Dahua ceiling applies** (~1.6 s observed on bench) |
| Network isolation | Two customer hubs / tower groups: no cross-customer traffic | Packet capture on managed switch |
| Tamper detection | Enclosure open → alert within 5 s | Interrupt-driven reed switch |
| Health monitoring | Stream fail → alert within 30 s | 10 s poll + 5 s ffprobe timeout |
| Sensor plugin | Second sensor type on bus without core changes | Phase 5 (ArtemisOS) |
| Buyer onboarding | Power on + claim → dashboard live | No customer terminal steps |

---

## 4. Proposed Architecture

### 4.1 Hardware Stack (Replacement Components)

| Component | Current | Proposed |
|-----------|---------|----------|
| Recorder/Compute | Commercial NVR + HDD | NVIDIA Jetson Orin NX (8/16GB) production; Orin Nano on bench |
| Storage | 3.5" spinning HDD | NVMe SSD (512GB min) — OS, logs, SMART; **not** v1 video archive |
| Network switch | Unmanaged or none | PoE+ managed switch (VLAN, ACL, mirror port) |
| WAN | None / ad hoc | **Wi-Fi** on Jetson (primary WAN); LTE/5G modem for field (automatic fallback) |
| Tamper sensors | None | MPU-6050 (I2C), magnetic reed switch, LDR |
| Cameras | Vendor PTZ (retained stock) | **ONVIF-compatible** (not brand-locked); N cameras per Jetson |

**Retained hardware:** Solar panels, charge controller, battery bank, enclosure shell, mounting structure.

**Jetson networking (production):** Single Ethernet port to managed switch (camera VLAN only). **Wi-Fi is the WAN** for internet, WireGuard egress, enrollment, and SSH debug. Ethernet and Wi-Fi must never share routing for camera traffic — enforced by switch VLANs, Jetson policy routing, and installer assertions.

### 4.2 Software Stack (Tower)

```
┌─────────────────────────────────────────────┐
│              ArtemisOS (Phase 5)            │  Inference, mission planner
├─────────────────────────────────────────────┤
│           Sensor Plugin Bus (gRPC)          │  Future sensors
├──────────────┬──────────────────────────────┤
│  PTZ Daemon  │    Watchdog Daemon           │  ONVIF PTZ + tamper/health
├──────────────┴──────────────────────────────┤
│  mediamtx (RTSP rebroadcast on wg0)         │  Live stream only (v1)
├─────────────────────────────────────────────┤
│          WireGuard wg0 (userspace on L4T)   │  Per-device keypair
├─────────────────────────────────────────────┤
│      Ubuntu 22.04 LTS — Jetson              │
└─────────────────────────────────────────────┘
```

### 4.3 Terra Platform (Control Plane)

```
┌─────────────────────────────────────────────┐
│           Terra Dashboard (buyer UI)          │  Live video + alerts
├─────────────────────────────────────────────┤
│  Alert ingest · RTSP integration contract   │
├─────────────────────────────────────────────┤
│  Enrollment API (HTTPS)                     │  First-boot + claim code
├─────────────────────────────────────────────┤
│  PostgreSQL registry (physical server)      │  customers, towers, IPs
├─────────────────────────────────────────────┤
│  Hub provisioner (HubProvider adapters)     │  One hub VM per customer org
└─────────────────────────────────────────────┘
```

Default hub hosting: **API-provisioned VPS** (AWS Lightsail first adapter; additional providers via `HubProvider` interface). Enterprise: **manual** hub on customer on-prem Ubuntu (Option C). No buyer-operated cloud consoles.

### 4.4 Network Architecture (Production)

```
[N × ONVIF Camera] ──PoE──┐
                          ├── [Managed Switch — Camera VLAN]
                          │      ACL: cameras → Jetson only; no internet
                          │
                    [Jetson eth]  ← camera VLAN only (no default route)
                          │
                    [Jetson Wi-Fi] ← WAN: internet, WG, enrollment, SSH
                          │
                    [WireGuard tunnel]
                          │
              [Customer hub — one per customer org]
                          │
                    [Terra Dashboard]
```

- **Camera VLAN:** No default gateway; no path to internet.
- **Jetson WAN:** Wi-Fi primary; LTE automatic fallback when a field modem is fitted (see roadmap Phase 5 field WAN).
- **Live video:** RTSP over WireGuard (`rtsp://<tower-vpn-ip>:8554/cam<n>`, TCP).
- **Alerts:** HMAC-signed JSON POST over WireGuard to hub listener → Terra dashboard pipeline.

### 4.5 Customer Integration Contract (v1)

| Outlet | Protocol | Consumer |
|--------|----------|----------|
| Live video | RTSP/TCP over VPN | Terra dashboard (and hub relay if needed) |
| Events | HTTP POST + `X-Kallon-Signature` (HMAC-SHA256) | Terra alert ingest → dashboard |

No customer-built NOC or VMS required for retail. Integration spec: `docs/alert-webhook.md` (roadmap deliverable).

---

## 5. Phased Work Plan

> **Status:** Phases 0–4 are **largely validated on bench** (see `kallon_current_state.md`). Remaining work is packaging (installer, registry, hub automation), pilot sign-off (managed switch, zero-egress proof), and field WAN. ArtemisOS remains Phase 5 below.

### Phase 0 — Bench Unit Assembly ✅

**Objective:** Representative bench unit mirroring production hardware.

**Status:** Complete on Jetson Orin Nano — camera ONVIF/RTSP, PTZ daemon, mediamtx, WireGuard, watchdog, tamper sensors verified live.

**Exit criteria:** Met. Baseline ONVIF enumeration and direct-cable path proven. Managed-switch captures deferred to Phase 1 pilot.

---

### Phase 1 — Data Sovereignty

**Objective:** Eliminate all camera cloud egress. Video flows locally to Jetson only.

**Tasks:**

*Camera-level (web UI):*
- Disable P2P, DMSS, Easy4IP, auto-update, email/FTP push
- NTP to local server or Jetson (avoid public NTP leakage where possible)

*Network-level (managed switch):*
- Camera VLAN; ACL cameras → Jetson IP only
- Jetson Ethernet port: camera VLAN only; **Wi-Fi carries all WAN traffic**
- Mirror port for Wireshark validation

*Validation:*
- 24h soak on mirror port: 0 bytes from cameras to non-Jetson destinations
- `baseline_capture_phase0.pcap` vs `post_isolation_capture_phase1.pcap`

**Exit criteria:**
- Zero-egress proof on **pilot unit** (once); identical camera/switch config assumed for subsequent production builds
- Local RTSP stable via `ffprobe` / mediamtx

---

### Phase 2 — Local PTZ Control

**Objective:** Local ONVIF PTZ with move-and-confirm semantics. No internet in PTZ path.

**Status:** Implemented — `dahua_onvif_control.py`, `sentry_ptz_absolute.py`, `kallon_ptz_daemon.py` (systemd on bench). Benchmark at n=10 shows ~1.6 s p95 (ONVIF stack limit); formal 1,000-command run and SLA re-baseline pending.

**Interface:**
- `move_absolute`, `move_continuous`, `get_position`, `move_with_confirm` (50 ms poll)
- JSON/TCP daemon on loopback for ArtemisOS consumption

**Exit criteria:**
- PTZ daemon operational as systemd service ✅
- 1,000-command benchmark documented; p95 target met **or** formally re-baselined for ONVIF cameras
- No PTZ traffic to external IPs ✅

---

### Phase 3 — Per-Customer Private Network

**Objective:** Each tower joins a dedicated WireGuard hub for its customer org. Cryptographic isolation between customers. Buyer sees live video in Terra dashboard.

**Tasks:**

*Tower (Jetson):*
- Per-device WG keypair; `AllowedIPs` scoped to customer subnet (not `0.0.0.0/0`)
- `wg-quick@wg0` + userspace drop-in on L4T; WG handshake watchdog (60 s)
- mediamtx RTSP rebroadcast on `wg0`; iptables restrict :8554 to `lo` + `wg0`
- First-boot **auto-enrollment** via HTTPS to Terra enrollment API; **claim code** links tower to customer org

*Terra platform:*
- Postgres registry on physical server; enrollment API
- **One hub VM per customer org** via `kallon-hub-provision` (HubProvider: Lightsail default; manual for enterprise)
- `kallon-gateway-add-peer` on hub when tower enrolls

*Validation:*
- WG auto-establishes on boot; auto-reconnect on drop ✅ (bench)
- Live RTSP over VPN to dashboard integration endpoint
- Two towers on one customer hub; two customer orgs: no cross-traffic (pcap)

**Exit criteria:**
- End-to-end live RTSP and alerts without hand-edited `wg0.conf`
- Terra test host off VPN cannot reach camera RTSP on LAN

---

### Phase 4 — Tamper Detection and Health Monitoring ✅

**Objective:** Real-time tamper and health alerts with HMAC-signed JSON to Terra alert pipeline.

**Status:** Implemented and verified on bench — MPU-6050, reed (pin 31), LDR (pin 33), RTSP/temp probes, HMAC end-to-end HTTP 200.

**Alert delivery:** `POST` to hub alert listener over WireGuard → Terra dashboard ingest (not a customer-operated webhook in retail flow).

**Exit criteria:** Met on bench — door alert &lt; 5 s; stream fail &lt; 30 s; HMAC verified; watchdog auto-restart ✅

---

### Phase 5 — Sensor Abstraction and ArtemisOS Integration

**Objective:** Plugin sensor bus; ArtemisOS consumes RTSP and telemetry; OTA pipeline.

**Prerequisite:** Phases 1–4 packaged and pilot-ready (roadmap Phases 1–4).

**Exit criteria:**
- Second sensor type registered without core changes
- ArtemisOS issuing PTZ via local daemon
- OTA bundle verified on bench

---

## 6. Bill of Materials (Bench / Pilot Unit)

| Item | Specification | Estimated Cost (USD) |
|------|--------------|----------------------|
| Edge compute | Jetson Orin NX 8GB + carrier (Nano for bench) | $250–$450 |
| Storage | NVMe SSD 512GB (M.2 2280) | $50–$80 |
| PoE+ managed switch | 5-port+, 802.3at, VLAN + mirror | $50–$120 |
| LTE modem | USB or M.2 + SIM (field WAN) | $40–$80 |
| Tamper sensors | MPU-6050, reed, LDR, resistors | $10–$20 |
| Ethernet / Wi-Fi | Cat6; Jetson uses onboard Wi-Fi for WAN | $15–$30 |
| Terra physical server | Registry + enrollment (existing) | — |
| Hub VPS | One small instance per customer org (Lightsail etc.) | ~$5–15/mo each |
| **Total estimate (tower hardware)** | | **$435–$700** |

*Cameras from production stock (ONVIF). Solar/power hardware out of scope.*

---

## 7. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Dahua firmware web server crash | Medium | High | Factory reset + firmware upgrade on isolated VLAN |
| P2P survives ACL via DNS | Low | High | Switch ACL + DNS block; verify with pcap |
| Wi-Fi WAN vs camera VLAN routing conflict | Medium | High | Single eth = camera VLAN only; default route only on Wi-Fi; installer assertions |
| WireGuard instability on LTE NAT | Medium | Medium | PersistentKeepalive 25 s; WG watchdog timer |
| PTZ latency above 100 ms target | High | Medium | Re-baseline for ONVIF; document; ArtemisOS path separate |
| VPS provider outage | Low | High | HubProvider adapters; hub per customer org limits blast radius |
| NVMe failure in enclosure heat | Low | High | Industrial NVMe; SMART via watchdog |

---

## 8. Deliverables Summary

| Phase | Deliverable |
|-------|-------------|
| 0 | Bench unit validated · ONVIF capability report ✅ |
| 1 | Zero-egress pcap (pilot once) · VLAN/ACL documentation |
| 2 | PTZ daemon · benchmark / re-baseline report |
| 3 | Provisioning + enrollment + hub automation · live RTSP to dashboard |
| 4 | Tamper wiring · watchdog · signed alert pipeline ✅ |
| 5 | Sensor plugin spec · ArtemisOS · OTA |

Manufacturing installer, registry schema, and hub provisioner: **`kallon_mass_deployment_roadmap.md`**.

---

## 9. Out of Scope (v1)

- Physical enclosure modifications (drilling, re-cabling, weatherproofing)
- Solar / power system changes
- Mandatory camera vendor replacement (ONVIF compatibility required; brand not fixed)
- **Video playback, DVR, and long-term historical archive**
- Customer-operated NOC, VMS, or cloud consoles (retail)
- Multi-tower orchestration beyond dashboard (ArtemisOS workstream)
- Per-tower cloud console provisioning

---

## 10. Revision History

| Version | Date | Author | Notes |
|---------|------|--------|-------|
| 1.0 | May 2026 | Terra Engineering | Initial brief |
| 2.0 | June 2026 | Terra Engineering | Buyer = Terra dashboard; control plane on physical server; Wi-Fi WAN + single eth camera VLAN; hub per customer org via HubProvider; Phases 0–4 bench status; no v1 playback; enrollment + claim code; roadmap cross-ref |

---

*Terra Industries · All Rights Reserved © 2026*
