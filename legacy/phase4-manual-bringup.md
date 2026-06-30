# Phase 4 — Getting the Alert System Running (Step-by-Step)

> **ARCHIVED — May 2025 manual bring-up.** Superseded by installer module `80-watchdogs.sh` and [`docs/field-test-setup.md`](../docs/field-test-setup.md) §5.
>
> **Follow instead:** [`docs/README.md`](../docs/README.md).

Everything below assumes:

- Jetson Orin Nano is on `192.168.1.246`, user `khalifa`, repo at `/home/khalifa/kallon`
- VPS is your WireGuard gateway (`10.50.0.1` over VPN)
- WireGuard tunnel is already working (Phase 3)
- Sensors are soldered to the breakout board and wired to J12 (reed → pin 31, LDR → pin 33, MPU SDA/SCL → pins 3/5, MPU INT → pin 29, 3.3V → pin 1, GND → pin 6)

You will need **two SSH sessions**: one to the Jetson, one to the VPS.

---

## Overview

```
                              WireGuard tunnel
   [Jetson]  ──────────────────────────────────────►  [VPS]
   192.168.1.246                                      10.50.0.1
   10.50.0.2 (wg0)                                    (wg0)

   kallon_watchdog.py                                 alert listener
   reads sensors                                      receives POSTs
   signs alert JSON (HMAC)  ── HTTP POST :8080 ──►    verifies & logs
   /etc/kallon/alert.key    ═══ same secret ═══       /etc/kallon/alert.key
```

The goal: open the enclosure door → see the alert appear on the VPS within seconds.

---

## Part A — Jetson Setup

### Step 1: SSH into the Jetson

```bash
ssh khalifa@192.168.1.246
```

### Step 2: Make sure the repo is up to date

```bash
cd /home/khalifa/kallon
git pull --ff-only origin main
```

You should see `kallon_watchdog.py`, `deploy/install-kallon-watchdog.sh`, etc.

### Step 3: Run the install script

This creates `/etc/kallon/`, writes a config template and a random HMAC key, installs Python deps, and copies the systemd unit.

```bash
cd /home/khalifa/kallon
sudo deploy/install-kallon-watchdog.sh
```

What it does behind the scenes:

| Action | Result |
|--------|--------|
| Adds `khalifa` to `gpio` and `i2c` groups | So the daemon can read sensors without root |
| Runs `pip3 install --user -r requirements.txt` | Installs `requests`, `smbus2`, `Jetson.GPIO` |
| Creates `/etc/kallon/device.env` | Config file with template values |
| Creates `/etc/kallon/alert.key` | Random 32-byte HMAC secret (base64-encoded) |
| Copies systemd unit to `/etc/systemd/system/` | So you can `systemctl enable` the watchdog |

### Step 4: Check the config file

```bash
sudo cat /etc/kallon/device.env
```

Review these values — **the defaults are probably fine for your bench**:

| Variable | Default | Change if... |
|----------|---------|--------------|
| `DEVICE_ID` | `kallon-unit-001` | You want a different name |
| `ALERT_WEBHOOK_URL` | `http://10.50.0.1:8080/alerts` | Your VPS uses a different port |
| `RTSP_URLS` | `rtsp://127.0.0.1:8554/cam1` | Your mediamtx path is different |
| GPIO / I2C pins | 31, 33, 29, bus 7 | You wired differently (you didn't) |

If anything needs changing:

```bash
sudo nano /etc/kallon/device.env
```

### Step 5: Verify the sensors are detected

Check the MPU-6050 is visible on I2C:

```bash
sudo i2cdetect -y -r 7
```

You should see `68` in the grid. If not, check your SDA/SCL wires on pins 3 and 5.

### Step 6: Dry-run the watchdog (sensors only, no network needed)

```bash
cd /home/khalifa/kallon
python3 kallon_watchdog.py --dry-run \
    --device-id kallon-unit-001 \
    --webhook-url http://10.50.0.1:8080/alerts \
    --alert-key-path /etc/kallon/alert.key
```

Look for these lines in the output:

```
MPU-6050 ready bus=7 addr=0x68 thr=20 dur=20ms
initial GPIO state door_open=False light_bright=False
dry-run complete; exiting before poll loop.
```

- `MPU-6050 ready` = accelerometer is talking over I2C
- `door_open=False` = reed switch sees the magnet (door closed)
- `light_bright=False` = LDR output is HIGH (dark, cover on — active-low module)

If `door_open=True` or `light_bright=True`, your sensor is reading the alarm state — physically check the magnet position and enclosure light.

**Do not start the service yet.** The VPS needs a listener first, or alerts will fail and get dropped.

### Step 7: Copy the HMAC key so you can put it on the VPS

```bash
sudo cat /etc/kallon/alert.key
```

This prints one line of base64 text. **Copy it** (select and copy from your terminal). You will paste it on the VPS in Part B.

---

## Part B — VPS Setup (Alert Listener)

### Step 8: SSH into the VPS

Open a **second terminal** on your PC:

```bash
ssh your-user@YOUR_VPS_PUBLIC_IP
```

### Step 9: Check WireGuard is up

```bash
sudo wg show
```

You should see a peer (the Jetson) and a recent handshake. Test connectivity:

```bash
ping -c 3 10.50.0.2
```

If this fails, WireGuard is down — fix that first (Phase 3 issue, not Phase 4).

### Step 10: Install the HMAC key on the VPS

```bash
sudo mkdir -p /etc/kallon
sudo nano /etc/kallon/alert.key
```

**Paste** the exact line you copied from the Jetson in Step 7. Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

Lock down permissions:

```bash
sudo chmod 640 /etc/kallon/alert.key
```

### Step 11: Verify the keys match

**On the VPS:**

```bash
sudo sha256sum /etc/kallon/alert.key
```

**On the Jetson** (switch to your other terminal):

```bash
sudo sha256sum /etc/kallon/alert.key
```

Compare the long hex string before the filename. **They must be identical.** If not, you pasted extra whitespace or a newline — redo Step 10.

### Step 12: Create a simple alert listener on the VPS

This is a small Python script that listens on port 8080, receives alert POSTs from the Jetson, verifies the HMAC signature, and prints them.

```bash
mkdir -p ~/kallon-noc
cat > ~/kallon-noc/alert_listener.py << 'PYEOF'
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
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"bad signature\n")
            print(f"[REJECTED] bad HMAC from {self.client_address[0]}")
            return

        alert = json.loads(body)
        severity = alert.get("severity", "?")
        alert_type = alert.get("alert_type", "?")
        device = alert.get("device_id", "?")
        details = json.dumps(alert.get("details", {}))
        print(f"[{severity}] {alert_type} from {device} — {details}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

print(f"Listening on {BIND[0]}:{BIND[1]} — Ctrl+C to stop")
HTTPServer(BIND, Handler).serve_forever()
PYEOF
```

### Step 13: Start the listener

```bash
sudo python3 ~/kallon-noc/alert_listener.py
```

You should see:

```
Listening on 0.0.0.0:8080 — Ctrl+C to stop
```

Leave this running. It will print every alert the Jetson sends.

> **Why `sudo`?** Only because the key file is root-owned. On a real NOC you would run this as a service with proper permissions. For the bench test this is fine.

---

## Part C — Start the Watchdog and Test

### Step 14: Go back to the Jetson terminal

### Step 15: Verify the Jetson can reach the listener

```bash
curl -v --max-time 5 http://10.50.0.1:8080/ 2>&1
```

**What you want to see:** any HTTP response — even `405 Method Not Allowed` or `501` means the listener is reachable and working. The exact error text does not matter; a response means the path is open.

**If it hangs or says "Connection timed out":**

```bash
# 1. Is WireGuard up?
sudo wg show
# Look for a recent "latest handshake" timestamp.

# 2. Can you reach the VPS at all?
ping -c 3 10.50.0.1

# 3. Is the listener actually running on the VPS?
# (switch to your VPS terminal — is it still printing "Listening on 0.0.0.0:8080"?)

# 4. Is the VPS firewall blocking port 8080?
# On the VPS:
sudo ufw status          # if ufw is active
sudo ufw allow 8080/tcp  # open it
```

### Step 16: Enable and start the watchdog

```bash
sudo systemctl enable --now kallon-watchdog
```

### Step 17: Watch the Jetson logs

```bash
journalctl -u kallon-watchdog -f
```

You should see startup lines:

```
starting kallon_watchdog device_id=kallon-unit-001 webhook=http://10.50.0.1:8080/alerts rtsp_count=1
MPU-6050 ready bus=7 addr=0x68 thr=20 dur=20ms
initial GPIO state door_open=False light_bright=False
entering poll loop interval=10.0s
```

If anything says `failed` or `error`, read the message — most common issues:

| Error | Fix |
|-------|-----|
| `DEVICE_ID is required` | Edit `/etc/kallon/device.env` |
| `alert key file ... is empty` | Check `/etc/kallon/alert.key` exists and is not empty |
| `MPU-6050 init failed` | Check I2C wiring; run `sudo i2cdetect -y -r 7` |
| `GPIO setup failed` | User not in `gpio` group; re-run install script or `sudo usermod -aG gpio khalifa` then reboot |

---

## Part D — Trigger Each Sensor

With the Jetson logs open in one terminal and the VPS listener in another, test each sensor:

### Test 1: Door reed switch

**Action:** Open the enclosure door (move the magnet away from the reed switch).

**Jetson log:** `alert sent type=TAMPER_DOOR_OPEN`

**VPS listener:** `[CRITICAL] TAMPER_DOOR_OPEN from kallon-unit-001 — {"gpio_pin": 31, "level": "HIGH"}`

**Close the door again.** VPS should show `[MEDIUM] TAMPER_DOOR_RECOVERED`.

### Test 2: LDR light sensor

**Action:** Shine a torch / phone flashlight onto the LDR (simulating cover removed).

**VPS listener:** `[CRITICAL] TAMPER_LIGHT from kallon-unit-001` (LDR output goes LOW when bright — active-low module)

**Remove the light.** VPS should show `TAMPER_LIGHT_RECOVERED`.

### Test 3: MPU-6050 impact / motion

**Action:** Tap or lift the enclosure.

**VPS listener:** `[HIGH] TAMPER_IMPACT from kallon-unit-001 — {"source": "mpu6050", ...}`

This one does not have a "recovered" alert — each impact is a one-shot event (deduped to once per 60 s).

### Test 4: Camera stream failure

**Action:** Unplug the camera's Ethernet cable.

**Wait ~30 seconds** (the poller checks every 10 s, ffprobe has a 5 s timeout).

**VPS listener:** `[HIGH] CAMERA_STREAM_FAIL from kallon-unit-001`

**Plug the camera back in.** After the next poll: `CAMERA_STREAM_RECOVERED`.

### Test 5: Temperature (hard to trigger naturally)

You probably won't hit 80 °C on the bench. To verify the code path works, you can temporarily lower the threshold:

```bash
sudo systemctl stop kallon-watchdog
sudo nano /etc/kallon/device.env
# Change TEMP_TRIGGER_C=40 and TEMP_CLEAR_C=35
sudo systemctl start kallon-watchdog
```

If the CPU is above 40 °C (likely), you will see `TEMP_CRITICAL`. **Change the values back** afterward.

---

## Part E — Confirm the Proof of Concept

### Checklist

Run through these and tick them off:

| # | Test | Expected on VPS | Pass? |
|---|------|-----------------|-------|
| 1 | Open door | `TAMPER_DOOR_OPEN` within ~1 s | |
| 2 | Close door | `TAMPER_DOOR_RECOVERED` | |
| 3 | Shine light on LDR | `TAMPER_LIGHT` within ~1 s | |
| 4 | Remove light | `TAMPER_LIGHT_RECOVERED` | |
| 5 | Tap / lift enclosure | `TAMPER_IMPACT` | |
| 6 | Unplug camera | `CAMERA_STREAM_FAIL` within ~30 s | |
| 7 | Plug camera back in | `CAMERA_STREAM_RECOVERED` | |
| 8 | Bad HMAC key → rejected | Change key on VPS, trigger alert → `[REJECTED]` | |

If all pass: **Phase 4 proof of concept is complete.**

---

## Troubleshooting

### "alert dropped after 3 attempts" in Jetson logs

The Jetson cannot reach the VPS listener.

```bash
# Check WireGuard
sudo wg show
ping -c 3 10.50.0.1

# Check the listener is running on the VPS
# (switch to VPS terminal — is it still printing "Listening on ..."?)

# Check VPS firewall
# On VPS:
sudo ufw allow 8080/tcp    # if ufw is active
```

### "bad signature" on VPS

Keys do not match. Re-do Steps 7, 10, 11.

### "MPU-6050 init failed" on startup

```bash
sudo i2cdetect -y -r 7
```

If nothing at `0x68`: check SDA (pin 3), SCL (pin 5), VCC (pin 1), GND (pin 6). Loose wire is the most common cause.

### "GPIO setup failed"

```bash
groups khalifa
# must include: gpio i2c
# if missing:
sudo usermod -aG gpio khalifa
sudo usermod -aG i2c khalifa
# then reboot (group changes need a new login session)
sudo reboot
```

### Watchdog crashes on startup

```bash
journalctl -u kallon-watchdog --no-pager | tail -50
```

Read the traceback. Common: missing Python package (`pip3 install --user requests smbus2 Jetson.GPIO`).

---

## Stopping / Restarting

```bash
# Stop
sudo systemctl stop kallon-watchdog

# Restart (after editing device.env)
sudo systemctl restart kallon-watchdog

# Disable (won't start on boot)
sudo systemctl disable kallon-watchdog

# View recent logs
journalctl -u kallon-watchdog --since "5 min ago"
```

---

## What is running where (summary)

| Machine | What | How |
|---------|------|-----|
| **Jetson** | `kallon-watchdog.service` | Reads sensors, signs alerts, POSTs to VPS |
| **VPS** | `alert_listener.py` | Receives alerts, verifies HMAC, prints to screen |
| **Both** | `/etc/kallon/alert.key` | Same secret — how the VPS trusts the Jetson |

---

*Terra Industries · Kallon Sentry Tower · Phase 4 Setup Guide*
