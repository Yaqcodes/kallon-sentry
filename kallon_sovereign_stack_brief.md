# Kallon Sentry Tower — Sovereign Stack Project Brief
**Terra Industries · Confidential**
Version 1.0 · May 2026

---

## 1. Executive Summary

The Kallon sentry tower currently operates on a commercially sourced NVR stack that introduces critical architectural vulnerabilities: third-party cloud video routing, unreliable PTZ control dependent on manufacturer relays, no per-customer network isolation, and no hardware-level tamper or health monitoring. This project brief documents the full redesign of the Kallon internal electronics and software stack to produce a fully sovereign, edge-native surveillance node — eliminating all third-party cloud dependencies and establishing the foundation for ArtemisOS integration and future sensor expansion.

The redesign does not affect the tower's physical shell, solar power system, or mounting hardware. The upgrade is self-contained to the compute, networking, and sensor electronics inside the enclosure.

---

## 2. Problem Statement

### 2.1 Current Architecture

The production Kallon unit consists of:

- **Cameras:** Dahua PTZ IP cameras (ONVIF-compatible)
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

The Dahua camera firmware runs multiple parallel access stacks by default: ONVIF (local, standards-based), Dahua P2P (cloud relay), DMSS cloud push, and the proprietary SDK layer. The NVR has been configured to use the cloud path rather than the local ONVIF path, creating the cloud dependency. The remediation path preserves the ONVIF stack and eliminates all others.

---

## 3. Goals and Success Criteria

### 3.1 Primary Goals

1. All video and telemetry data remains within the customer's private network at all times — no data transits Terra or any third-party infrastructure after provisioning
2. PTZ control operates fully locally with deterministic sub-100ms command latency
3. Each customer deployment is cryptographically isolated via per-device WireGuard VPN
4. Physical tampering and component faults are detected and reported in real time
5. New sensors can be integrated without modifying core firmware

### 3.2 Success Criteria (Exit Conditions)

| Criterion | Measurement |
|-----------|-------------|
| Zero outbound camera traffic to third-party IPs | Wireshark packet capture: 0 bytes to Dahua/cloud IPs during 24h soak test |
| PTZ command latency | p95 < 100ms over 1,000 consecutive commands |
| Network isolation | Two customer VPN tunnels on same switch: no cross-tunnel traffic visible |
| Tamper detection | Enclosure open event generates NOC alert within 5 seconds |
| Health monitoring | Camera stream failure detected and alerted within 30 seconds |
| Sensor plugin | Second sensor type registered and publishing data without core changes |

---

## 4. Proposed Architecture

### 4.1 Hardware Stack (Replacement Components)

| Component | Current | Proposed |
|-----------|---------|----------|
| Recorder/Compute | Commercial NVR + spinning HDD | NVIDIA Jetson Orin NX (8/16GB) |
| Storage | 3.5" spinning HDD | NVMe SSD (512GB min) |
| Network switch | Unmanaged or none | PoE+ managed switch |
| Cellular uplink | None / unknown | LTE/5G USB modem or M.2 module |
| Tamper sensors | None | MPU-6050 (I2C), magnetic reed switch, photodiode |
| Cameras | Dahua PTZ (retained) | Dahua PTZ — ONVIF-only mode, cloud disabled |

**Retained hardware:** Solar panels, charge controller, battery bank, enclosure shell, mounting structure.

### 4.2 Software Stack

```
┌─────────────────────────────────────────────┐
│              ArtemisOS                      │  Threat classification, mission planner,
│         (inference + C2 layer)              │  multi-device orchestration
├─────────────────────────────────────────────┤
│           Sensor Plugin Bus                 │  gRPC interface — sensor-agnostic
│     (RTSP adapter · future sensors)         │
├──────────────┬──────────────────────────────┤
│  PTZ Control │    Watchdog Daemon           │  Local ONVIF driver + health monitor
│   Daemon     │    (systemd service)         │
├──────────────┴──────────────────────────────┤
│          WireGuard VPN (wg0)                │  Per-device keypair, per-customer gateway
├─────────────────────────────────────────────┤
│      Ubuntu 22.04 LTS — Jetson Orin NX      │  Secure boot, signed OTA updates
└─────────────────────────────────────────────┘
```

### 4.3 Network Architecture

```
[Dahua Camera] ──PoE──┐
[Dahua Camera] ──PoE──┤
                  [Managed Switch]
                       │
                 [Jetson Orin NX]
                       │
               [LTE/5G Modem]
                       │
              [WireGuard Tunnel]
                       │
          [Customer VPN Gateway]
                       │
            [Customer NOC / VMS]
```

- Camera VLAN: cameras isolated to local subnet, no default gateway, no internet access
- Jetson is the sole device with WAN access, via the cellular modem
- All customer data travels exclusively over the encrypted WireGuard tunnel
- Terra has zero visibility into customer data streams post-provisioning

---

## 5. Phased Work Plan

### Phase 0 — Bench Unit Assembly
**Duration:** 1 week
**Owner:** Hardware engineer
**Prerequisite:** None

**Objective:** Assemble a complete representative bench unit that mirrors production hardware. All subsequent phases are validated on this unit before any changes are made to deployed towers.

**Tasks:**
- Source Jetson Orin NX module and carrier board (Seeed reComputer or equivalent)
- Source NVMe SSD (512GB), PoE+ managed switch, LTE USB modem
- Obtain 1–2 production Dahua PTZ cameras
- Wire bench rig: cameras → PoE switch → Jetson; isolated LAN (no internet uplink initially)
- Document factory state: run Wireshark on switch mirror port, capture all camera traffic for 30 minutes; save as `baseline_capture_phase0.pcap`
- Enumerate camera ONVIF capabilities using `onvif-zeep` or SOAPUI; document all supported profiles and PTZ services
- Confirm camera default credentials and firmware version

**Exit criteria:**
- Bench unit powered and cabled
- Baseline packet capture saved and reviewed
- ONVIF device service endpoint confirmed reachable at `http://<camera-ip>/onvif/device_service`

**Known issue to resolve first:** Current bench cameras are intermittently unreachable — visible in ConfigTool for a short window after boot then dropping off. Diagnose before proceeding:
1. Connect camera directly to laptop via ethernet; assign static IP `192.168.1.50/24` to laptop
2. Run continuous ping to `192.168.1.108` from power-on
3. If ping drops after 30–60s: firmware web server crash — perform factory reset and upgrade firmware
4. If ping never responds: IP conflict or subnet mismatch — use `arp -a` to find real camera IP
5. Factory reset procedure: hold reset pinhole 10–15s with power on; complete initial setup on isolated network with internet blocked

---

### Phase 1 — Data Sovereignty
**Duration:** 2 weeks
**Owner:** Embedded Linux / networking engineer
**Prerequisite:** Phase 0 complete; camera reliably reachable

**Objective:** Eliminate all Dahua cloud egress. Video must flow exclusively over local RTSP to the Jetson. Zero bytes to Dahua or third-party IPs.

**Tasks:**

*Camera-level (Dahua web interface):*
- Disable P2P: Network → Platform Access → P2P → Off
- Disable DMSS cloud push
- Disable Easy4IP / Dahua cloud registration
- Disable auto-update
- Disable all email/FTP alert push
- Set NTP to local server or Jetson IP (prevents DNS leakage)

*Network-level (managed switch):*
- Create camera VLAN; assign camera ports to VLAN
- Set VLAN ACL: cameras may only communicate with Jetson IP; all other destinations dropped
- No default gateway configured on camera subnet

*Validation:*
- Run 24-hour Wireshark soak test on switch mirror port
- Confirm zero outbound camera packets to any destination except Jetson IP
- Confirm RTSP stream is pullable: `ffprobe rtsp://<camera-ip>/cam/realmonitor?channel=1&subtype=0`
- Record stream to file using FFmpeg, verify video quality and continuity

**Exit criteria:**
- 24h packet capture shows 0 bytes to Dahua/cloud IPs
- RTSP stream stable and recordable locally
- Comparison document: `baseline_capture_phase0.pcap` vs `post_isolation_capture_phase1.pcap`

---

### Phase 2 — Local PTZ Control
**Duration:** 2 weeks
**Owner:** Software engineer (Python / embedded)
**Prerequisite:** Phase 1 complete

**Objective:** Implement fully local, deterministic PTZ control with command acknowledgment. Sub-100ms p95 latency. No internet dependency.

**Tasks:**

*Implementation:*
- Install `onvif-zeep` on Jetson: `pip3 install onvif-zeep`
- Implement PTZ control module with the following interface:
  - `move_absolute(pan, tilt, zoom)` — moves to absolute position
  - `move_continuous(pan_vel, tilt_vel, zoom_vel, duration)` — continuous motion
  - `get_position()` — returns current pan/tilt/zoom from `ptz.GetStatus()`
  - `move_with_confirm(pan, tilt, zoom, timeout_ms=500)` — issues `AbsoluteMove`, polls `GetStatus` at 50ms intervals until position confirmed or timeout
- Wrap PTZ module as a `systemd` service with auto-restart on failure
- Expose PTZ control over local Unix socket or gRPC for consumption by ArtemisOS

*Validation:*
- Write benchmark script: issue 1,000 `AbsoluteMove` commands to randomised positions; record round-trip time for each
- Generate latency report: mean, p50, p95, p99, max
- Target: p95 < 100ms

**Exit criteria:**
- PTZ control module operational as `systemd` service
- 1,000-command benchmark complete; p95 latency < 100ms documented
- No PTZ command path touches any external IP

---

### Phase 3 — Per-Customer Private Network
**Duration:** 2 weeks
**Owner:** DevOps / networking engineer
**Prerequisite:** Phase 1 complete (can run in parallel with Phase 2)

**Objective:** Each Kallon unit establishes an encrypted WireGuard tunnel to the customer's private gateway. No Terra infrastructure in the data path. Cryptographic isolation between customers.

**Tasks:**

*Device side (Jetson):*
- Install WireGuard: `apt install wireguard`
- Write provisioning script: generates unique Ed25519 keypair per device, writes `/etc/wireguard/wg0.conf`
- Configure `wg0` interface: device VPN IP, customer gateway endpoint, `AllowedIPs` scoped to customer subnet only (not `0.0.0.0/0`)
- Enable as `systemd` service: `systemctl enable wg-quick@wg0`
- Implement auto-reconnect watchdog: if `wg0` interface has no handshake in 60s, restart

*Customer gateway side:*
- Document self-hosted gateway setup: WireGuard on Ubuntu VPS or on-premises server
- Write peer configuration template for each Kallon device
- Test two simultaneous device tunnels; confirm no cross-tunnel visibility

*Video and telemetry routing:*
- Configure RTSP rebroadcast: `mediamtx` (formerly rtsp-simple-server) on Jetson listens on `wg0` IP
- Customer NOC opens RTSP stream via VPN tunnel IP
- Validate end-to-end: VLC on customer NOC machine opens live stream

**Exit criteria:**
- WireGuard tunnel auto-establishes on boot and auto-reconnects on drop
- Customer NOC can view live RTSP stream exclusively over VPN tunnel
- Two customer tunnels on same bench switch: packet capture confirms zero cross-tunnel data
- Terra test machine (not on VPN) cannot reach camera stream

---

### Phase 4 — Tamper Detection and Health Monitoring
**Duration:** 2 weeks
**Owner:** Embedded hardware + software engineer
**Prerequisite:** Phase 3 complete (alert pipeline requires VPN tunnel)

**Objective:** Detect physical tampering and component faults in real time. Signed alerts delivered to customer NOC within defined SLA.

**Tasks:**

*Hardware additions:*
- Wire MPU-6050 accelerometer to Jetson 40-pin GPIO header via I2C (SDA → pin 3, SCL → pin 5, VCC → 3.3V, GND → GND)
- Wire magnetic reed switch to GPIO input pin with pull-up resistor (enclosure door detection)
- Wire photodiode or LDR to GPIO input (internal light intrusion detection)
- Document wiring schematic; add to hardware revision notes

*Watchdog daemon:*
- Implement `systemd` service polling at 10-second intervals:
  - **RTSP health:** `ffprobe` against each camera's local RTSP URL; fail if no response in 5s
  - **CPU temperature:** read `/sys/class/thermal/thermal_zone*/temp`; alert if > 80°C
  - **NVMe health:** parse `smartctl -A /dev/nvme0` for reallocated sectors and temperature
  - **Power voltage:** read ADC if available; alert on undervoltage
  - **Accelerometer:** detect sustained g-force above threshold (impact/tilt)
  - **Door reed switch:** GPIO state change triggers immediate alert (no polling delay)
  - **Light sensor:** GPIO state change triggers immediate alert
- All alerts serialised as signed JSON payload (HMAC-SHA256, shared key provisioned at install)
- Alert delivery: HTTP POST to customer NOC webhook endpoint over WireGuard tunnel

*Alert schema:*
```json
{
  "device_id": "kallon-unit-001",
  "timestamp_utc": "2026-05-14T10:23:44Z",
  "alert_type": "TAMPER_DOOR_OPEN | CAMERA_STREAM_FAIL | TEMP_CRITICAL | DISK_FAULT | IMPACT",
  "severity": "CRITICAL | HIGH | MEDIUM",
  "details": {},
  "hmac": "<sha256-signature>"
}
```

**Exit criteria:**
- Open enclosure door → NOC receives alert within 5 seconds
- Kill RTSP stream (unplug camera) → NOC receives alert within 30 seconds
- Watchdog daemon auto-restarts on crash (confirmed via `systemd` restart policy)
- Alert HMAC verification passes on NOC side

---

### Phase 5 — Sensor Abstraction and ArtemisOS Integration
**Duration:** Ongoing (initial milestone: 3 weeks)
**Owner:** Software architect + ArtemisOS team
**Prerequisite:** Phases 1–4 complete

**Objective:** Define a stable sensor plugin interface that allows new hardware to be added without core firmware changes. Integrate Jetson video pipeline and telemetry bus with ArtemisOS.

**Tasks:**

*Sensor plugin API:*
- Define gRPC service: `SensorService` with methods `Register()`, `Publish()`, `Subscribe()`
- Implement RTSP adapter as first plugin: wraps Jetson's local RTSP streams, publishes decoded frames to sensor bus
- Implement telemetry adapter as second plugin: publishes watchdog health data to sensor bus
- Document plugin contract: any new sensor driver that implements `SensorService` is automatically available to the inference pipeline

*ArtemisOS hookup:*
- ArtemisOS subscribes to sensor bus via `SensorService.Subscribe()`
- AI inference pipeline consumes video frames from RTSP adapter
- Threat detections trigger PTZ tracking commands via PTZ control daemon
- Mission planner issues autonomous patrol waypoints via PTZ daemon

*OTA update pipeline:*
- Terra holds Ed25519 signing keypair for firmware bundles
- Jetson runs update agent that polls Terra update server (over dedicated WireGuard peer, not customer tunnel)
- Update bundles verified against public key before application
- Failed update rolls back automatically; success logged and reported

*Documentation:*
- Sensor plugin developer guide
- ArtemisOS integration API reference
- OTA signing and distribution runbook

**Exit criteria:**
- Second sensor type (e.g. simulated radar driver) registered and publishing data without core changes
- ArtemisOS consuming RTSP frames and issuing PTZ commands via local daemon
- OTA update applied and verified on bench unit end-to-end

---

## 6. Bill of Materials (Bench Unit)

| Item | Specification | Estimated Cost (USD) |
|------|--------------|----------------------|
| Edge compute | NVIDIA Jetson Orin NX 8GB + carrier board | $250–$350 |
| Storage | NVMe SSD 512GB (M.2 2280) | $50–$80 |
| PoE+ managed switch | 5-port, 802.3at, VLAN support | $50–$80 |
| LTE modem | USB or M.2 LTE/5G module + SIM | $40–$80 |
| Tamper sensors | MPU-6050, reed switch, photodiode, resistors | $10–$20 |
| Ethernet cables | Cat6, various lengths | $15–$20 |
| USB accessories | Keyboard, HDMI adapter (setup only) | $20–$30 |
| **Total estimate** | | **$435–$660** |

*Cameras are sourced from existing production stock. Solar/power hardware is not part of this scope.*

---

## 7. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Dahua firmware web server crash preventing ONVIF access | Medium | High | Factory reset + firmware upgrade on isolated network; document procedure |
| Dahua P2P process survives network ACL and persists via DNS | Low | High | DNS blackhole for Dahua domains on switch; OEM firmware as fallback |
| Jetson GPIO incompatibility with sensor hardware | Low | Medium | Validate I2C wiring on bench before enclosure integration |
| WireGuard tunnel instability on cellular uplink | Medium | Medium | Auto-reconnect watchdog; keep-alive ping every 25s |
| Customer NOC unable to host self-managed VPN gateway | Medium | Low | Provide hosted gateway option under customer-specific Tailscale or self-hosted WG server |
| NVMe failure in high-temperature enclosure | Low | High | Industrial-grade NVMe rated to 85°C; S.M.A.R.T. monitoring as early warning |

---

## 8. Deliverables Summary

| Phase | Deliverable |
|-------|-------------|
| 0 | Wired bench unit · baseline packet capture · ONVIF capability report |
| 1 | 24h zero-egress packet capture · local RTSP validated · isolation documentation |
| 2 | PTZ control daemon · 1,000-command latency benchmark report |
| 3 | WireGuard provisioning scripts · customer gateway setup guide · end-to-end VPN video test |
| 4 | Tamper sensor wiring schematic · watchdog daemon · alert pipeline · signed alert spec |
| 5 | Sensor plugin API spec · ArtemisOS integration · OTA pipeline · developer documentation |

---

## 9. Out of Scope

- Physical enclosure modifications (drilling, re-cabling, weatherproofing)
- Solar / power system changes
- Camera hardware replacement (Dahua units are retained; ONVIF mode is sufficient)
- Customer NOC dashboard or VMS software
- Multi-tower orchestration (handled by ArtemisOS as a separate workstream)
- Production deployment — this brief covers the bench validation unit only

---

## 10. Revision History

| Version | Date | Author | Notes |
|---------|------|--------|-------|
| 1.0 | May 2026 | Terra Engineering | Initial brief — derived from architecture review and diagnostic sessions |

---

*Terra Industries · All Rights Reserved © 2026*
