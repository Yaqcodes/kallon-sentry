# Kallon Hardware Wiring — Rev A

Phase 4 tamper and health-monitoring sensors on a Jetson Orin Nano dev kit.

Read alongside `kallon_sovereign_stack_brief.md` (Phase 4) and `kallon_watchdog.py`.

---

## 1. Bill of materials (per unit)

| Item | Notes |
|------|-------|
| Jetson Orin Nano dev kit | 40-pin expansion header (J12) |
| MPU-6050 breakout | I2C, INT pin broken out |
| Magnetic reed switch | Normally-open; closes when magnet is present |
| Digital LDR module | On-board comparator with digital OUT |
| 10 kΩ resistor | Pull-up for the reed line |
| Hookup wire, breakout PCB | All three sensors on one solder breakout |

The MPU-6050 breakout already has 2.2 kΩ pull-ups on SDA/SCL, so no extra I2C pull-ups are needed.

---

## 2. Pin assignment (Jetson Orin Nano J12, BOARD numbering)

| Function | J12 pin | Header name | Notes |
|----------|---------|-------------|-------|
| 3.3 V power to sensor board | **1** | 3.3 V | Up to 1 A available on the dev kit |
| GND | **6** | GND | Any GND pin works; pin 6 is closest to the I2C pair |
| MPU SDA | **3** | I2C1_SDA | I2C **bus 7** on Orin Nano (`i2cdetect -y -r 7`) |
| MPU SCL | **5** | I2C1_SCL | |
| MPU INT | **29** | GPIO01 | Active-high pulse from MPU |
| Reed switch signal | **31** | GPIO11 | Pull-up to 3.3 V, other contact to GND |
| LDR digital OUT | **33** | GPIO13 | Module is powered from pin 1 / GND |

I2C addresses observed: MPU-6050 at `0x68` (AD0 low). If you tie AD0 high it becomes `0x69`; update `MPU_I2C_ADDR` in `/etc/kallon/device.env` if you do.

Pin numbers above match the defaults baked into `kallon_watchdog.py` and the env file written by `install-kallon-watchdog.sh`.

---

## 3. Sensor signal logic

| Sensor | Idle state on the wire | Alarm state | Alert types |
|--------|------------------------|-------------|-------------|
| Reed switch | Door closed → magnet present → contacts closed → **GPIO LOW** | Door open → contacts open → pull-up wins → **GPIO HIGH** | `TAMPER_DOOR_OPEN` / `TAMPER_DOOR_RECOVERED` |
| Digital LDR (active-low) | Inside enclosure is dark → module OUT **HIGH** | Cover removed / light intrusion → OUT **LOW** | `TAMPER_LIGHT` / `TAMPER_LIGHT_RECOVERED` |
| MPU-6050 | Steady, gravity-only signal → INT idle LOW | Motion above threshold → INT pulse HIGH | `TAMPER_IMPACT` |

> Note on the reed wording: the line reads 3.3 V when the switch is open (door open or magnet not present) because the pull-up holds it high. When the door is closed and the magnet brings the contacts together, the line is shorted to GND and reads 0 V. The watchdog treats **HIGH = door open** and fires `TAMPER_DOOR_OPEN` on the LOW→HIGH transition.

### Reed wiring detail

```
  3.3V (pin 1) ---[10k]---+---> GPIO pin 31
                          |
                          +---[reed switch]---> GND (pin 6)
```

- Magnet present (door closed): switch shorted → GPIO reads 0 V (LOW)
- Magnet absent (door open):    switch open    → pull-up wins, GPIO reads 3.3 V (HIGH)

The LDR module is **active-low**: OUT goes LOW when light is detected, HIGH when dark. The watchdog treats **LOW = bright (alarm)** and **HIGH = dark (normal)**. If your module is the opposite, swap `GPIO.LOW` / `GPIO.HIGH` in `_on_ldr` inside `kallon_watchdog.py`.

### MPU-6050 motion-detection settings

Defaults in `kallon_watchdog.py`:

| Register | Value | Meaning |
|----------|-------|---------|
| `PWR_MGMT_1` (0x6B) | reset, then 0x00 | Wake from sleep, internal 8 MHz clock |
| `ACCEL_CONFIG` (0x1C) | 0x01 | ±2 g full-scale, HPF 5 Hz (rejects steady gravity) |
| `INT_PIN_CFG` (0x37) | 0x10 | Active-high, push-pull, 50 µs pulse, cleared on any read |
| `INT_ENABLE` (0x38) | 0x40 | Motion-detection interrupt only |
| `MOT_DUR` (0x20) | 20 | Motion must persist 20 ms |
| `MOT_THR` (0x1F) | 20 | ~20 mg trigger threshold |

These values pick up someone lifting or shaking the enclosure but ignore normal road / wind vibration on a fixed pole. Tune `MOT_THR` upward (try 40–60) if you see false positives, or downward if the unit fails to detect a lift.

---

## 4. Pins explicitly avoided

| Pins | Reason |
|------|--------|
| 2, 4 | 5 V rail; sensors here are 3.3 V only |
| 8, 10 | UART1 (`/dev/ttyTHS0`) — reserved for serial console / future use |
| 27, 28 | I2C0 / bus 1 — carrier has existing devices on `0x25` and `0x40` |
| 19, 21, 23, 24, 26, 37 | SPI; reserved for a future expansion sensor |

---

## 5. Bench bring-up checklist

Run with the Jetson **powered off** while wiring, then power on.

1. `sudo i2cdetect -y -r 7` — expect `0x68` (or `0x69`).
2. `cat /sys/class/gpio/gpiochip*/label` — confirm the kernel sees GPIOs.
3. Run the watchdog in dry-run mode to validate config and wiring:
   ```bash
   sudo systemctl stop kallon-watchdog 2>/dev/null || true
   sudo -u khalifa --preserve-env=DEVICE_ID,ALERT_WEBHOOK_URL,ALERT_KEY_PATH \
       /usr/bin/python3 /home/khalifa/kallon/kallon_watchdog.py --dry-run
   ```
   Look for `MPU-6050 ready` and `initial GPIO state ...` log lines.
4. Open the enclosure door: NOC webhook should receive `TAMPER_DOOR_OPEN` within ~1 s.
5. Shine a torch through the enclosure seam: expect `TAMPER_LIGHT`.
6. Lift / tap the enclosure: expect `TAMPER_IMPACT`.
7. Disconnect the camera Ethernet: expect `CAMERA_STREAM_FAIL` within ~30 s.

Close conditions should produce the matching `*_RECOVERED` alerts.

---

*Terra Industries · Kallon Sentry Tower · Hardware revision A*
