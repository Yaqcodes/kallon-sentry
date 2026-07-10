# Dahua ONVIF script — how to run and test

## One-time setup

Open PowerShell (or Command Prompt), go to this folder, install dependencies:

```powershell
cd "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE"
pip install -r requirements.txt
```

## Defaults (from the script)

- **Host:** `192.168.1.108`
- **User:** `admin`
- **Password:** `terra123` (unless you set `CAMERA_PASSWORD` or pass `-p`)

**Camera encode (provisioning):** for reliable kiosk video, set substream to H.264
in each camera's web UI when you provision them (Dahua:
**Setup → Camera → Video → Encode** → substream). The tower rebroadcast path
(`subtype=1` in `CAMERA_RTSP_PATH`) must match the substream you configure.

If ONVIF does not answer on port **80**, try **8899**:

```powershell
python dahua_onvif_control.py --port 8899
```

## Deterministic PTZ (main path)

For **AbsoluteMove** plus **GetStatus** confirmation (50 ms polling, tolerance-based) and a **benchmark** of many moves to fixed poses, use **`sentry_ptz_absolute.py`** (same defaults and WSDL layout as `dahua_onvif_control.py`). The older script keeps **ContinuousMove** for simple nudges; this one is for repeatable, confirmed positioning on a **local** LAN.

```powershell
python sentry_ptz_absolute.py status
python sentry_ptz_absolute.py move --pan 0.2 --tilt 0
python sentry_ptz_absolute.py benchmark --count 100
python sentry_ptz_absolute.py --help
```

Requires a PTZ profile and camera firmware that supports **ONVIF AbsoluteMove** and reports position in **GetStatus** (not all models do).

### PTZ daemon (Jetson / systemd)

**`kallon_ptz_daemon.py`** holds one lazily-opened ONVIF session **per camera**
and serves **newline-delimited JSON** over TCP (default `127.0.0.1:8765`) or
`--unix /path` on Linux. Set **`CAMERA_PASSWORD`** in the environment for
production. See the script docstring for the `method` / `params` schema
(`ping`, `list_cameras`, `status`, `move_absolute`, `move_continuous`, `stop`,
`home`).

**Camera selection.** Every camera-facing method accepts an optional **1-based
`camera`** param that matches the order of `CAMERA_IPS` in `device.env` (so
`camera` N == mediamtx path `camN`). It **defaults to `1`** when omitted, so
existing single-camera callers are unchanged. Query the wiring with
`list_cameras`.

- **Service (production):** cameras are read from `CAMERA_IPS`,
  `CAMERA_RTSP_USER`, `CAMERA_PASSWORD`, and `CAMERA_ONVIF_PORT` in
  `device.env` (loaded via the unit's `EnvironmentFile`). No `--host` is
  passed; single- and multi-camera towers share one code path.
- **Bench (single camera):** pass `--host <ip>` to override the environment;
  that camera becomes index `1`.

```powershell
cd "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE"
$env:CAMERA_PASSWORD = "your_password"
python kallon_ptz_daemon.py --host 192.168.1.108      # bench: single camera → index 1
```

Example requests (each returns one JSON line), e.g. with `nc` on Linux:

```bash
echo '{"id":1,"method":"ping","params":{}}' | nc -q 1 127.0.0.1 8765
echo '{"id":2,"method":"list_cameras","params":{}}' | nc -q 1 127.0.0.1 8765
# nudge camera 2 (pan right ~0.4s), then stop it:
echo '{"id":3,"method":"move_continuous","params":{"camera":2,"pan":0.3,"tilt":0,"zoom":0,"seconds":0.4}}' | nc -q 1 127.0.0.1 8765
echo '{"id":4,"method":"stop","params":{"camera":2}}' | nc -q 1 127.0.0.1 8765
```

The installer (`scripts/install/80-watchdogs.sh`) renders
`/etc/systemd/system/kallon-ptz-daemon.service` for you. For a manual bench
install, copy **`deploy/kallon-ptz-daemon.service.example`** to
`/etc/systemd/system/`, edit paths and `ExecStart`, then
`systemctl enable --now kallon-ptz-daemon`.

### Health & tamper watchdog (Phase 4, Jetson Orin Nano)

**`kallon_watchdog.py`** is a single systemd-managed daemon that watches the camera, CPU temperature, and the tamper sensors wired to the 40-pin header (MPU-6050 on I2C bus 7, reed switch on pin 31, digital LDR on pin 33, MPU INT on pin 29). It posts HMAC-SHA256-signed JSON alerts to the customer NOC over the WireGuard tunnel, with up to 3 retries and a 60 s per-type dedup window.

Pin assignments and sensor logic are documented in **`docs/hardware-wiring.md`** (Rev A).

Production towers use the modular installer (`scripts/kallon-jetson-install.sh`, module `80-watchdogs.sh`). For a manual bench install:

```bash
cd /home/khalifa/kallon
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env --only-module 80

# or enable after full install:
sudo systemctl enable --now kallon-watchdog
journalctl -u kallon-watchdog -f
```

Ensure `/etc/kallon/device.env` and `/etc/kallon/alert.key` are installed on the
tower with correct permissions before running the installer (see
`docs/identity-and-secrets.md` §3.2; bench walkthrough: `docs/field-test-setup.md` §5).

Bench check without enabling the service yet:

```bash
sudo -u khalifa /usr/bin/python3 /home/khalifa/kallon/kallon_watchdog.py --dry-run
```

NVMe and power-voltage checks are implemented but disabled by default; flip `ENABLE_NVME=1` in `/etc/kallon/device.env` once an SSD is installed. The power-ADC check stays inert until an actual voltage monitor exists on the carrier.

## Command format (what the numbers mean)

General shape (brackets mean optional; `|` means “pick one”):

```text
python dahua_onvif_control.py [OPTIONS] [ACTION]
```

- **`ACTION`** — What to do: `test` (default), `info`, `profiles`, `rtsp`, `snapshot`, `ptz`, `stop`, or `home`. You can put options before or after the action.

```text
python dahua_onvif_control.py [--host ADDR] [-P PORT] [-u USER] [-p PASSWORD]
    [--profile N] [--timeout SEC] [--wsdl-dir PATH]
    [ptz-specific: --pan A --tilt B --zoom Z --seconds T]
    [ACTION]
```

### Options (names and types)

| Option | Meaning |
|--------|--------|
| `--host` | Camera IP or hostname (text). |
| `-P` / `--port` | **Integer:** TCP port for **ONVIF over HTTP** on the camera (often `80` or `8899`). Not the RTSP video port. |
| `-u` / `--user` | Login name (text). |
| `-p` / `--password` | Login password (text). `CAMERA_PASSWORD` in the environment overrides the default from the script. |
| `--profile` | **Integer (0, 1, 2, …):** which **media profile** to use—the same index order as `profiles` prints. Used by `rtsp`, `snapshot`, `ptz`, `stop`, and `home`. Default `0`. |
| `--timeout` | **Number (seconds, can be decimal):** longest time to wait for each ONVIF HTTP request before failing. Default `15.0`. |
| `--wsdl-dir` | Folder path (text) that contains `devicemgmt.wsdl` if the bundled `wsdl` folder is not next to the script. |

### PTZ-only numbers (`ptz` action)

These are **normalized velocities** for ONVIF **ContinuousMove** (not degrees on the lens). Range is about **-1.0 to 1.0**; larger magnitude = faster in that axis. **`0`** means no movement on that axis.

| Option | Meaning |
|--------|--------|
| `--pan` | Horizontal speed: **negative** vs **positive** = opposite directions along the pan axis (which is “left/right” depends on mount and firmware). |
| `--tilt` | Vertical speed: **negative** vs **positive** = opposite directions along the tilt axis. |
| `--zoom` | Zoom speed: **positive** usually zooms **in**, **negative** **out** (if the camera exposes zoom on this profile). |
| `--seconds` | **Number (seconds, can be decimal):** how long to run the continuous move **before** the script sends **Stop**. Default `1.0`. |

Example: `--pan 0.3 --seconds 1.5` means “pan at speed 0.3 for 1.5 seconds, then stop.”

### Actions that ignore some options

- **`test`**, **`info`**, **`profiles`** — use connection options only; no PTZ numbers.
- **`rtsp`**, **`snapshot`** — use `--profile` (and connection options).
- **`stop`**, **`home`** — use `--profile` (and connection options).
- **`ptz`** — uses `--profile` plus `--pan`, `--tilt`, `--zoom`, and `--seconds`.

---

## Test that ONVIF works (recommended first step)

Runs the default **`test`** action: connects, prints device summary, profile count, and an **RTSP** URL for profile `0`.

```powershell
python dahua_onvif_control.py
```

Same thing, spelled out:

```powershell
python dahua_onvif_control.py test
```

Override host or password for a quick check:

```powershell
python dahua_onvif_control.py --host 192.168.1.108 -p terra123 test
```

Use an environment variable for the password (overrides the script default):

```powershell
$env:CAMERA_PASSWORD = "your_password_here"
python dahua_onvif_control.py test
```

---

## Controlling the camera

Actions are the first argument: `ptz`, `stop`, `home`, `rtsp`, `snapshot`, etc. If you omit it, the script runs **`test`**.

**PTZ behavior:** `ptz` sends a **continuous** move for the axis you set, waits **`--seconds`**, then sends **Stop**. Use **`stop`** if movement ever sticks. Values are roughly **-1..1** (start small, e.g. `0.2`).

**Pan / tilt / zoom (typical signs — your firmware may invert):**

- **Pan:** positive `x` → one horizontal direction, negative → the other.
- **Tilt:** positive `y` → one vertical direction, negative → the other.
- **Zoom:** positive → zoom in, negative → zoom out (when supported).

### Find which profile has PTZ

```powershell
python dahua_onvif_control.py profiles
```

If `ptz` says the profile has no PTZ, try **`--profile 1`** (or another index from that list).

### PTZ: one axis at a time

Pan right ~1.5 s (default profile 0):

```powershell
python dahua_onvif_control.py ptz --pan 0.3 --seconds 1.5
```

Pan left:

```powershell
python dahua_onvif_control.py ptz --pan -0.3 --seconds 1.5
```

Tilt up / tilt down:

```powershell
python dahua_onvif_control.py ptz --tilt 0.25 --seconds 1
python dahua_onvif_control.py ptz --tilt -0.25 --seconds 1
```

Zoom in / zoom out (PTZ camera with zoom only):

```powershell
python dahua_onvif_control.py ptz --zoom 0.2 --seconds 1.2
python dahua_onvif_control.py ptz --zoom -0.2 --seconds 1.2
```

### PTZ: diagonal move (pan + tilt together)

```powershell
python dahua_onvif_control.py ptz --pan 0.2 --tilt 0.15 --seconds 2
```

### PTZ on another stream / non-default camera

Same commands with **host**, **ONVIF HTTP port**, and **profile index**:

```powershell
python dahua_onvif_control.py --host 192.168.1.50 -P 8899 ptz --profile 1 --pan 0.25 --seconds 1
```

### Stop and home

Emergency stop (uses default `--profile 0`; add `--profile` if needed):

```powershell
python dahua_onvif_control.py stop
python dahua_onvif_control.py stop --profile 1
```

Return to home preset (if the camera supports ONVIF home):

```powershell
python dahua_onvif_control.py home
python dahua_onvif_control.py home --profile 0
```

### Small “tour” (run several moves in a row)

PowerShell example: short right, short up, then home.

```powershell
python dahua_onvif_control.py ptz --pan 0.25 --seconds 1
python dahua_onvif_control.py ptz --tilt 0.2 --seconds 1
python dahua_onvif_control.py home
```

### Streams and stills (not PTZ, but useful for monitoring)

Print RTSP URL for VLC or an NVR:

```powershell
python dahua_onvif_control.py rtsp
python dahua_onvif_control.py rtsp --profile 1
```

Print snapshot (JPEG) URL to paste in a browser or `curl`:

```powershell
python dahua_onvif_control.py snapshot
```

### If WSDL errors appear (`devicemgmt.wsdl`)

Keep the project’s `wsdl` folder next to `dahua_onvif_control.py`, or set an explicit path:

```powershell
python dahua_onvif_control.py --wsdl-dir "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\CODE\wsdl" test
```

---

## Other useful commands

**Device info (short):**

```powershell
python dahua_onvif_control.py info
```

**List media profiles (indices and tokens):**

```powershell
python dahua_onvif_control.py profiles
```

**Print RTSP URL (default profile 0):**

```powershell
python dahua_onvif_control.py rtsp
```

**RTSP for profile 1:**

```powershell
python dahua_onvif_control.py rtsp --profile 1
```

**Snapshot (JPEG) URL:**

```powershell
python dahua_onvif_control.py snapshot
```

**PTZ, stop, home, multi-step moves, and alternate host/port/profile:** see **[Controlling the camera](#controlling-the-camera)** above.

---

## If something fails

1. **Camera web UI:** enable **ONVIF** and ensure the user is allowed for ONVIF.
2. **Firewall / VLAN:** your PC must reach the camera IP (try `ping 192.168.1.108`).
3. **Wrong port:** add `-P 8899` (or the port shown in the camera’s ONVIF settings).
4. **PTZ errors:** run `profiles` and try `--profile 0` or `1` on the stream that has PTZ.

---

## Help

```powershell
python dahua_onvif_control.py --help
```
