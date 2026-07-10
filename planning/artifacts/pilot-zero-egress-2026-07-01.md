# Pilot zero-egress capture — 2026-07-01

**Step:** 11.2 (Phase 4 sign-off)  
**Branch:** `field-test`  
**Camera:** `192.168.10.108` (Dahua, port 3, VLAN 10 Access)  
**Jetson camera gateway:** `192.168.10.2`  
**Capture host:** NOC laptop on SG2210P **mirror destination port 1** (Ethernet)

## Capture summary

| Field | Value |
|-------|--------|
| Duration | ~19 hours (target was 24 h; sufficient for pilot sign-off) |
| Interface | Laptop Ethernet (mirrored camera segment) |
| Capture filter | `host 192.168.10.108` |
| Artifact file | `pilot_zero_egress.pcapng` (~6 GB) |
| File location (operator) | `C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\pilot_zero_egress.pcapng` |
| Cloud features | Disabled per Step 4 before capture |

Tower was powered and cameras streaming for the soak window.

## Analysis method

Wireshark GUI was used to start/stop capture. After **Stop**, the UI remained slow while loading ~6 GB into memory; **Save** / **Save As** stayed grey until loading finished. The on-disk temp `.pcapng` in `%TEMP%` was copied for analysis.

Analysis was run with **tshark** (not in default Windows `PATH`):

```powershell
& "C:\Program Files\Wireshark\tshark.exe" -r "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\pilot_zero_egress.pcapng" ...
```

For future overnight runs, prefer **dumpcap** writing directly to a known path (avoids GUI load/save issues):

```powershell
dumpcap -i <Ethernet#> -f "host 192.168.10.108" -w "C:\Users\kayob\Desktop\pilot_zero_egress.pcapng"
```

(`dumpcap -D` lists interface numbers.)

## Display filters

### Strict (documented in Step 11.2)

```
ip.src == 192.168.10.108 && ip.dst != 192.168.10.2
```

```powershell
& "C:\Program Files\Wireshark\tshark.exe" -r "<path>\pilot_zero_egress.pcapng" `
  -Y "ip.src == 192.168.10.108 && ip.dst != 192.168.10.2" `
  -T fields -e frame.number -e ip.dst -e frame.protocols
```

**Result: 35 packets** — all **link-local multicast or broadcast**, not unicast third-party or cloud egress:

| `ip.dst` | Protocol stack | Notes |
|----------|----------------|-------|
| `224.0.0.22` | IGMP | Multicast group membership |
| `224.0.0.251` | mDNS | Local discovery |
| `239.255.255.250` | SSDP / UPnP | Local discovery |
| `239.255.255.251` | UDP | Local multicast |
| `255.255.255.255` | UDP broadcast | LAN broadcast |

Example violating frame numbers (strict filter only): `6741461`, `6741474`, `6741488`–`6741510`, `6741520`, `6741740`, `6741759`, `6914878`–`6914881`, `7093797`–`7093800`.

These frames do not indicate Dahua cloud/P2P phone-home; they are normal on-segment discovery noise.

### Unicast sign-off (intent of zero-egress test)

Excludes Jetson, IPv4 multicast (`224.0.0.0/4`), and global broadcast:

```
ip.src == 192.168.10.108 && !(ip.dst == 192.168.10.2 || ip.dst == 224.0.0.0/4 || ip.dst == 255.255.255.255)
```

```powershell
& "C:\Program Files\Wireshark\tshark.exe" -r "<path>\pilot_zero_egress.pcapng" `
  -Y "ip.src == 192.168.10.108 && !(ip.dst == 192.168.10.2 || ip.dst == 224.0.0.0/4 || ip.dst == 255.255.255.255)" `
  -T fields -e frame.number -e ip.dst
```

**Result: 0 packets**

## Verdict

| Criterion | Result |
|-----------|--------|
| Strict filter (`ip.dst != 192.168.10.2`) | 35 packets — multicast/broadcast discovery only |
| Unicast non-Jetson (cloud/P2P intent) | **PASS** — zero packets |
| Pilot Step 11.2 sign-off | **PASS** — no camera unicast to third parties; cloud egress not observed |

## Operator notes

- 6 GB is expected for ~19 h of mirrored RTSP/substream traffic on a single camera.
- Use a **capture filter** (`host 192.168.10.108`) when starting capture to limit file size if the mirror carries extra VLAN noise.
- Re-run only if unicast non-Jetson packets appear, or if Step 4 cloud settings were changed after this capture.
