#!/usr/bin/env python3
"""
Kallon health & tamper watchdog — long-running daemon for Jetson Orin Nano.

Phase 4 of the sovereign stack brief.

What it monitors
----------------
- RTSP camera streams        : ffprobe, 10 s interval (state-tracked, recovered alerts)
- CPU temperature            : /sys/class/thermal/thermal_zone*, 10 s interval,
                               80/75 deg C hysteresis (recovered alerts)
- MPU-6050 motion / impact   : I2C bus 7, polled accel delta every poll interval
- Magnetic reed door switch  : GPIO pin 31 (HIGH = door open)
- Digital LDR cover sensor   : GPIO pin 33 (active-low: LOW = bright / cover removed)
- NVMe SMART health          : DISABLED by default (no SSD on bench yet)
- Power undervoltage via ADC : DISABLED (no ADC on bench)

Alert format
------------
- JSON body with device_id, timestamp_utc (RFC 3339 UTC), nonce (uuid4),
  alert_type, severity, details.
- Canonical JSON (sorted keys, no spaces). HMAC-SHA256 over the canonical body.
- Header: X-Kallon-Signature: sha256=<hex>
- Delivery: HTTP POST to ALERT_WEBHOOK_URL over WireGuard.
- Up to 3 send attempts with exponential backoff (1 s, 2 s, 4 s).
- 60 s dedup window per alert_type prevents alert storms.

Configuration
-------------
Read from environment (typically set via /etc/kallon/device.env in systemd):

  DEVICE_ID              required, e.g. kallon-unit-001
  ALERT_WEBHOOK_URL      required, e.g. http://10.50.0.1:8080/alerts
  ALERT_KEY_PATH         path to shared HMAC key file (default /etc/kallon/alert.key)
  RTSP_URLS              comma-separated list, e.g. rtsp://127.0.0.1:8554/cam1
  POLL_INTERVAL_SEC      default 10
  TEMP_TRIGGER_C         default 80
  TEMP_CLEAR_C           default 75
  DEDUP_WINDOW_SEC       default 60
  MPU_I2C_BUS            default 7  (Orin Nano J12 pins 3/5)
  MPU_I2C_ADDR           default 0x68
  GPIO_REED_PIN          default 31 (BOARD numbering)
  GPIO_LDR_PIN           default 33
  MPU_ACCEL_THRESHOLD_MG default 150  (delta from last reading to trigger impact)
  ENABLE_NVME            default 0   (set 1 once SSD is installed)
  NVME_DEVICE            default /dev/nvme0
  ENABLE_POWER_ADC       default 0   (no ADC on Orin Nano dev kit)

Optional tower lab dashboard integration (off by default; a normal fleet
tower is unaffected). See docs/tower-lab-dashboard.md.

  ALERT_WEBHOOK_URL_LOCAL  if set, every alert is ALSO POSTed here (best-effort,
                           independent of the primary hub delivery). Used to
                           mirror alerts to a local listener on lab towers.
  ENABLE_TOWER_DASHBOARD   default 0; when 1, turns the status API on by default.
  TOWER_STATUS_API_ENABLE  default = ENABLE_TOWER_DASHBOARD; expose GET /status
                           and GET /healthz on loopback (read-only snapshot of
                           the watchdog's current in-memory sensor state).
  TOWER_STATUS_API_HOST    default 127.0.0.1 (loopback only — never the network)
  TOWER_STATUS_API_PORT    default 8770

CLI overrides match the env names (lowercased, dashes).

Run as a non-root user that is a member of the `gpio` and `i2c` groups.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import hashlib
import hmac
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("kallon_watchdog")


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------


class Severity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


class AlertType(str, enum.Enum):
    TAMPER_DOOR_OPEN = "TAMPER_DOOR_OPEN"
    TAMPER_DOOR_RECOVERED = "TAMPER_DOOR_RECOVERED"
    TAMPER_LIGHT = "TAMPER_LIGHT"
    TAMPER_LIGHT_RECOVERED = "TAMPER_LIGHT_RECOVERED"
    TAMPER_IMPACT = "TAMPER_IMPACT"
    CAMERA_STREAM_FAIL = "CAMERA_STREAM_FAIL"
    CAMERA_STREAM_RECOVERED = "CAMERA_STREAM_RECOVERED"
    TEMP_CRITICAL = "TEMP_CRITICAL"
    TEMP_RECOVERED = "TEMP_RECOVERED"
    DISK_FAULT = "DISK_FAULT"
    POWER_UNDERVOLT = "POWER_UNDERVOLT"


SEVERITY_BY_TYPE: dict[AlertType, Severity] = {
    AlertType.TAMPER_DOOR_OPEN: Severity.CRITICAL,
    AlertType.TAMPER_LIGHT: Severity.CRITICAL,
    AlertType.TAMPER_IMPACT: Severity.HIGH,
    AlertType.CAMERA_STREAM_FAIL: Severity.HIGH,
    AlertType.TEMP_CRITICAL: Severity.HIGH,
    AlertType.DISK_FAULT: Severity.HIGH,
    AlertType.POWER_UNDERVOLT: Severity.MEDIUM,
    AlertType.TAMPER_DOOR_RECOVERED: Severity.MEDIUM,
    AlertType.TAMPER_LIGHT_RECOVERED: Severity.MEDIUM,
    AlertType.CAMERA_STREAM_RECOVERED: Severity.MEDIUM,
    AlertType.TEMP_RECOVERED: Severity.MEDIUM,
}


@dataclasses.dataclass
class Alert:
    alert_type: AlertType
    details: dict[str, Any]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Config:
    device_id: str
    webhook_url: str
    alert_key: bytes
    rtsp_urls: list[str]
    poll_interval_sec: float
    temp_trigger_c: float
    temp_clear_c: float
    dedup_window_sec: float
    mpu_i2c_bus: int
    mpu_i2c_addr: int
    gpio_reed_pin: int
    gpio_ldr_pin: int
    mpu_accel_threshold_mg: float
    enable_nvme: bool
    nvme_device: str
    enable_power_adc: bool
    # Optional tower lab dashboard integration (off by default).
    webhook_url_local: str = ""
    status_api_enable: bool = False
    status_api_host: str = "127.0.0.1"
    status_api_port: int = 8770


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def load_config(args: argparse.Namespace) -> Config:
    device_id = args.device_id or os.environ.get("DEVICE_ID", "")
    webhook_url = args.webhook_url or os.environ.get("ALERT_WEBHOOK_URL", "")
    key_path = args.alert_key_path or os.environ.get(
        "ALERT_KEY_PATH", "/etc/kallon/alert.key"
    )
    if not device_id:
        raise SystemExit("DEVICE_ID is required (env or --device-id).")
    if not webhook_url:
        raise SystemExit("ALERT_WEBHOOK_URL is required (env or --webhook-url).")

    key_bytes = Path(key_path).read_bytes().strip()
    if not key_bytes:
        raise SystemExit(f"Alert key file {key_path} is empty.")

    rtsp_csv = args.rtsp_urls or os.environ.get("RTSP_URLS", "")
    rtsp_urls = [u.strip() for u in rtsp_csv.split(",") if u.strip()]

    # Optional tower lab dashboard hooks. The status API is enabled implicitly
    # when the dashboard is enabled, or explicitly via TOWER_STATUS_API_ENABLE.
    dashboard_enabled = _env_bool("ENABLE_TOWER_DASHBOARD", False)
    status_api_enable = _env_bool("TOWER_STATUS_API_ENABLE", dashboard_enabled)
    # Mirror alerts to the local listener so the dashboard's alert panel works
    # out of the box when the dashboard is enabled. Explicit env always wins.
    webhook_url_local = os.environ.get("ALERT_WEBHOOK_URL_LOCAL", "").strip()
    if not webhook_url_local and dashboard_enabled:
        webhook_url_local = "http://127.0.0.1:8080/alerts"

    return Config(
        device_id=device_id,
        webhook_url=webhook_url,
        alert_key=key_bytes,
        rtsp_urls=rtsp_urls,
        poll_interval_sec=_env_float("POLL_INTERVAL_SEC", 10.0),
        temp_trigger_c=_env_float("TEMP_TRIGGER_C", 80.0),
        temp_clear_c=_env_float("TEMP_CLEAR_C", 75.0),
        dedup_window_sec=_env_float("DEDUP_WINDOW_SEC", 60.0),
        mpu_i2c_bus=_env_int("MPU_I2C_BUS", 7),
        mpu_i2c_addr=int(os.environ.get("MPU_I2C_ADDR", "0x68"), 0),
        gpio_reed_pin=_env_int("GPIO_REED_PIN", 31),
        gpio_ldr_pin=_env_int("GPIO_LDR_PIN", 33),
        mpu_accel_threshold_mg=_env_float("MPU_ACCEL_THRESHOLD_MG", 150.0),
        enable_nvme=_env_bool("ENABLE_NVME", False),
        nvme_device=os.environ.get("NVME_DEVICE", "/dev/nvme0"),
        enable_power_adc=_env_bool("ENABLE_POWER_ADC", False),
        webhook_url_local=webhook_url_local,
        status_api_enable=status_api_enable,
        status_api_host=os.environ.get("TOWER_STATUS_API_HOST", "127.0.0.1"),
        status_api_port=_env_int("TOWER_STATUS_API_PORT", 8770),
    )


# ---------------------------------------------------------------------------
# Status store + optional loopback status API (tower lab dashboard)
# ---------------------------------------------------------------------------


class StatusStore:
    """Thread-safe snapshot of the watchdog's current in-memory sensor state.

    This does NOT add any polling: the probes/handlers push the values they
    already compute each cycle (or on GPIO edges) into here, and the optional
    status API just serves the latest snapshot. When the dashboard is off the
    store is still updated (cheap) but never served.
    """

    def __init__(self, config: Config) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._data: dict[str, Any] = {
            "device_id": config.device_id,
            "poll_interval_sec": config.poll_interval_sec,
            "mpu_present": False,
            "door": {"open": None},
            "light": {"exposed": None},
            "impact": {
                "threshold_mg": config.mpu_accel_threshold_mg,
                "last_delta_mg": None,
                "last_impact_utc": None,
            },
            "temperature": {
                "celsius": None,
                "zone": None,
                "critical": False,
                "trigger_c": config.temp_trigger_c,
                "clear_c": config.temp_clear_c,
            },
            "disk": {"enabled": config.enable_nvme, "faulted": False},
            "streams": [],
        }

    def update(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def merge(self, key: str, patch: dict[str, Any]) -> None:
        with self._lock:
            current = self._data.get(key)
            if isinstance(current, dict):
                current.update(patch)
            else:
                self._data[key] = dict(patch)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = json.loads(json.dumps(self._data))  # cheap deep copy
        snap["timestamp_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snap["uptime_sec"] = round(time.monotonic() - self._started, 1)
        return snap


class StatusServer:
    """Read-only loopback HTTP server exposing the watchdog status snapshot.

    Stdlib only (mirrors infra/hub/alert_listener.py). GET /status and
    GET /healthz. Runs in a daemon thread; failures never affect monitoring.
    """

    def __init__(self, store: StatusStore, host: str, port: int) -> None:
        self._store = store
        self._host = host
        self._port = port
        self._httpd: Any = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        store = self._store

        class Handler(BaseHTTPRequestHandler):
            server_version = "kallon-watchdog-status/1.0"

            def _reply(self, code: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    self._reply(200, {"status": "ok"})
                elif self.path == "/status":
                    self._reply(200, store.snapshot())
                else:
                    self._reply(404, {"error": "not found"})

            def log_message(self, *args: Any) -> None:  # silence access logging
                pass

        self._httpd = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="status-api", daemon=True
        )
        self._thread.start()
        LOG.info("status API listening on %s:%d", self._host, self._port)

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Alert sender (HMAC, retry, dedup)
# ---------------------------------------------------------------------------


class AlertSender:
    """Background sender. Thread-safe via an internal queue."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._q: queue.Queue[Optional[Alert]] = queue.Queue()
        self._last_sent: dict[AlertType, float] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="alert-sender", daemon=True)
        # Import lazily so the module is testable without the network library.
        import requests  # noqa: WPS433
        self._requests = requests

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=5.0)

    def submit(self, alert: Alert) -> None:
        """Enqueue an alert; dedup is applied just before sending."""
        self._q.put(alert)

    def _is_duplicate(self, alert_type: AlertType, now: float) -> bool:
        with self._lock:
            last = self._last_sent.get(alert_type)
            if last is not None and (now - last) < self.config.dedup_window_sec:
                return True
            self._last_sent[alert_type] = now
            return False

    def _build_payload(self, alert: Alert) -> dict[str, Any]:
        return {
            "device_id": self.config.device_id,
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nonce": str(uuid.uuid4()),
            "alert_type": alert.alert_type.value,
            "severity": SEVERITY_BY_TYPE[alert.alert_type].value,
            "details": alert.details,
        }

    def _sign(self, body: bytes) -> str:
        digest = hmac.new(self.config.alert_key, body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def _post_once(
        self, url: str, body: bytes, signature: str, timeout: float
    ) -> tuple[bool, str]:
        try:
            resp = self._requests.post(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Kallon-Signature": signature,
                },
                timeout=timeout,
            )
            if 200 <= resp.status_code < 300:
                return True, f"http_{resp.status_code}"
            return False, f"http_{resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"net:{exc.__class__.__name__}"

    def _deliver(
        self, url: str, label: str, alert_type: str, body: bytes, signature: str, attempts: int
    ) -> None:
        backoff = 1.0
        last_reason = ""
        for attempt in range(1, attempts + 1):
            ok, reason = self._post_once(url, body, signature, timeout=5.0)
            last_reason = reason
            if ok:
                LOG.info(
                    "alert sent dest=%s type=%s attempt=%d status=%s",
                    label, alert_type, attempt, reason,
                )
                return
            LOG.warning(
                "alert send failed dest=%s type=%s attempt=%d reason=%s",
                label, alert_type, attempt, reason,
            )
            if attempt < attempts:
                time.sleep(backoff)
                backoff *= 2.0
        LOG.error(
            "alert dropped dest=%s after %d attempts type=%s last_reason=%s",
            label, attempts, alert_type, last_reason,
        )

    def _send_with_retry(self, alert: Alert) -> None:
        payload = self._build_payload(alert)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = self._sign(body)
        alert_type = alert.alert_type.value

        # Primary hub delivery (unchanged: 3 attempts with backoff).
        self._deliver(self.config.webhook_url, "hub", alert_type, body, signature, attempts=3)

        # Optional local mirror for the tower lab dashboard. Independent of the
        # hub result so neither destination can block the other. Loopback, so a
        # short 2-attempt budget is plenty.
        if self.config.webhook_url_local:
            self._deliver(
                self.config.webhook_url_local, "local", alert_type, body, signature, attempts=2
            )

    def _run(self) -> None:
        while True:
            alert = self._q.get()
            if alert is None:
                return
            if self._is_duplicate(alert.alert_type, time.monotonic()):
                LOG.info("alert suppressed (dedup) type=%s", alert.alert_type.value)
                continue
            try:
                self._send_with_retry(alert)
            except Exception:  # noqa: BLE001
                LOG.exception("unhandled error sending alert type=%s", alert.alert_type.value)


# ---------------------------------------------------------------------------
# MPU-6050 driver (motion-detection interrupt)
# ---------------------------------------------------------------------------


MPU_PWR_MGMT_1 = 0x6B
MPU_ACCEL_XOUT_H = 0x3B
_MPU_CYCLE_BIT = 0x20  # bit 5 of PWR_MGMT_1: low-power cycle mode
_MPU_SLEEP_BIT = 0x40  # bit 6 of PWR_MGMT_1


class MPU6050:
    """Minimal MPU-6050 driver: wake + raw accel read.

    Hardware motion-detection registers (0x1F/0x20) are broken on many
    clone chips, so we poll accel readings and detect motion in software.
    """

    def __init__(self, bus_num: int, address: int) -> None:
        from smbus2 import SMBus  # noqa: WPS433
        self._bus = SMBus(bus_num)
        self._addr = address

    def _wake(self) -> None:
        self._bus.write_byte_data(self._addr, MPU_PWR_MGMT_1, 0x00)
        time.sleep(0.05)

    def init(self) -> None:
        b = self._bus
        a = self._addr
        b.write_byte_data(a, MPU_PWR_MGMT_1, 0x80)  # reset
        time.sleep(0.1)
        self._wake()

    def _ensure_awake(self) -> None:
        """Re-wake the MPU if it slipped into sleep or returned all-zeros."""
        pwr = self._bus.read_byte_data(self._addr, MPU_PWR_MGMT_1)
        if pwr & (_MPU_SLEEP_BIT | _MPU_CYCLE_BIT):
            LOG.warning("MPU in sleep/cycle (PWR_MGMT_1=0x%02X); re-waking", pwr)
            self._wake()

    def read_accel_g(self) -> tuple[float, float, float]:
        raw = self._bus.read_i2c_block_data(self._addr, MPU_ACCEL_XOUT_H, 6)
        if raw == [0, 0, 0, 0, 0, 0]:
            LOG.warning("MPU returned all-zero accel; checking power state")
            self._ensure_awake()
            time.sleep(0.02)
            raw = self._bus.read_i2c_block_data(self._addr, MPU_ACCEL_XOUT_H, 6)
        def s16(hi: int, lo: int) -> int:
            v = (hi << 8) | lo
            return v - 0x10000 if v & 0x8000 else v
        x = s16(raw[0], raw[1]) / 16384.0
        y = s16(raw[2], raw[3]) / 16384.0
        z = s16(raw[4], raw[5]) / 16384.0
        return x, y, z

    def close(self) -> None:
        try:
            self._bus.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# GPIO + interrupt handlers
# ---------------------------------------------------------------------------


class GpioHandlers:
    """Reed switch, LDR, and MPU INT — edge-triggered via Jetson.GPIO."""

    def __init__(
        self, config: Config, sender: AlertSender, status: Optional[StatusStore] = None
    ) -> None:
        self.config = config
        self.sender = sender
        self.status = status
        # State for recovered alerts. None = not yet known.
        self._door_open: Optional[bool] = None
        self._light_bright: Optional[bool] = None
        import Jetson.GPIO as GPIO  # noqa: WPS433
        self._gpio = GPIO

    def setup(self) -> None:
        GPIO = self._gpio
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        # Reed: door open = HIGH, door closed = LOW. External pull-up to 3V3 already on board.
        GPIO.setup(self.config.gpio_reed_pin, GPIO.IN)
        # LDR: active-low module — bright = LOW, dark = HIGH.
        GPIO.setup(self.config.gpio_ldr_pin, GPIO.IN)
        # Seed initial states so we only alert on real transitions.
        self._door_open = GPIO.input(self.config.gpio_reed_pin) == GPIO.HIGH
        self._light_bright = GPIO.input(self.config.gpio_ldr_pin) == GPIO.LOW
        LOG.info(
            "initial GPIO state door_open=%s light_bright=%s",
            self._door_open,
            self._light_bright,
        )
        if self.status is not None:
            self.status.merge("door", {"open": self._door_open})
            self.status.merge("light", {"exposed": self._light_bright})
        # If we boot with door open or cover off, fire one alert immediately.
        if self._door_open:
            self.sender.submit(Alert(
                AlertType.TAMPER_DOOR_OPEN,
                {"gpio_pin": self.config.gpio_reed_pin, "level": "HIGH", "boot_state": True},
            ))
        if self._light_bright:
            self.sender.submit(Alert(
                AlertType.TAMPER_LIGHT,
                {"gpio_pin": self.config.gpio_ldr_pin, "level": "LOW", "boot_state": True},
            ))

        GPIO.add_event_detect(
            self.config.gpio_reed_pin,
            GPIO.BOTH,
            callback=self._on_reed,
            bouncetime=50,
        )
        GPIO.add_event_detect(
            self.config.gpio_ldr_pin,
            GPIO.BOTH,
            callback=self._on_ldr,
            bouncetime=50,
        )

    def teardown(self) -> None:
        try:
            self._gpio.cleanup()
        except Exception:  # noqa: BLE001
            pass

    # Callbacks run on a background thread inside Jetson.GPIO.
    def _on_reed(self, channel: int) -> None:
        is_high = self._gpio.input(channel) == self._gpio.HIGH
        if is_high and self._door_open is not True:
            self._door_open = True
            if self.status is not None:
                self.status.merge("door", {"open": True})
            self.sender.submit(Alert(
                AlertType.TAMPER_DOOR_OPEN,
                {"gpio_pin": channel, "level": "HIGH"},
            ))
        elif not is_high and self._door_open is not False:
            self._door_open = False
            if self.status is not None:
                self.status.merge("door", {"open": False})
            self.sender.submit(Alert(
                AlertType.TAMPER_DOOR_RECOVERED,
                {"gpio_pin": channel, "level": "LOW"},
            ))

    def _on_ldr(self, channel: int) -> None:
        is_low = self._gpio.input(channel) == self._gpio.LOW
        if is_low and self._light_bright is not True:
            self._light_bright = True
            if self.status is not None:
                self.status.merge("light", {"exposed": True})
            self.sender.submit(Alert(
                AlertType.TAMPER_LIGHT,
                {"gpio_pin": channel, "level": "LOW"},
            ))
        elif not is_low and self._light_bright is not False:
            self._light_bright = False
            if self.status is not None:
                self.status.merge("light", {"exposed": False})
            self.sender.submit(Alert(
                AlertType.TAMPER_LIGHT_RECOVERED,
                {"gpio_pin": channel, "level": "HIGH"},
            ))



# ---------------------------------------------------------------------------
# Pollers (RTSP, temperature, NVMe)
# ---------------------------------------------------------------------------


class RtspProbe:
    def __init__(
        self, config: Config, sender: AlertSender, status: Optional[StatusStore] = None
    ) -> None:
        self.config = config
        self.sender = sender
        self.status = status
        self._failed: dict[str, bool] = {url: False for url in config.rtsp_urls}

    def _publish(self) -> None:
        if self.status is None:
            return
        streams = [
            {"path": f"cam{i}", "url": url, "ok": not self._failed.get(url, False)}
            for i, url in enumerate(self.config.rtsp_urls, start=1)
        ]
        self.status.update("streams", streams)

    def probe_once(self) -> None:
        for url in self.config.rtsp_urls:
            ok, exit_code, stderr_excerpt = self._ffprobe(url)
            was_failed = self._failed.get(url, False)
            if not ok and not was_failed:
                self._failed[url] = True
                self.sender.submit(Alert(
                    AlertType.CAMERA_STREAM_FAIL,
                    {"url": url, "exit_code": exit_code, "stderr_excerpt": stderr_excerpt},
                ))
            elif ok and was_failed:
                self._failed[url] = False
                self.sender.submit(Alert(
                    AlertType.CAMERA_STREAM_RECOVERED,
                    {"url": url},
                ))
        self._publish()

    @staticmethod
    def _ffprobe(url: str) -> tuple[bool, int, str]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return False, -1, "ffprobe not installed"
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    ffprobe,
                    "-v", "error",
                    "-rtsp_transport", "tcp",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "csv=p=0",
                    url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                check=False,
            )
            ok = proc.returncode == 0 and proc.stdout.strip() != b""
            stderr_excerpt = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
            excerpt = stderr_excerpt[0] if stderr_excerpt else ""
            return ok, proc.returncode, excerpt[:200]
        except subprocess.TimeoutExpired:
            return False, -2, "ffprobe timeout 5s"
        except Exception as exc:  # noqa: BLE001
            return False, -3, f"{exc.__class__.__name__}: {exc}"[:200]


class TemperatureProbe:
    def __init__(
        self, config: Config, sender: AlertSender, status: Optional[StatusStore] = None
    ) -> None:
        self.config = config
        self.sender = sender
        self.status = status
        self._in_critical = False

    def probe_once(self) -> None:
        hottest = self._read_hottest_zone()
        if hottest is None:
            return
        zone, celsius = hottest
        if celsius >= self.config.temp_trigger_c and not self._in_critical:
            self._in_critical = True
            self.sender.submit(Alert(
                AlertType.TEMP_CRITICAL,
                {"zone": zone, "celsius": round(celsius, 1), "threshold_c": self.config.temp_trigger_c},
            ))
        elif celsius < self.config.temp_clear_c and self._in_critical:
            self._in_critical = False
            self.sender.submit(Alert(
                AlertType.TEMP_RECOVERED,
                {"zone": zone, "celsius": round(celsius, 1), "threshold_c": self.config.temp_clear_c},
            ))
        if self.status is not None:
            self.status.merge(
                "temperature",
                {"celsius": round(celsius, 1), "zone": zone, "critical": self._in_critical},
            )

    @staticmethod
    def _read_hottest_zone() -> Optional[tuple[str, float]]:
        base = Path("/sys/class/thermal")
        if not base.exists():
            return None
        hottest: Optional[tuple[str, float]] = None
        for zone_dir in sorted(base.glob("thermal_zone*")):
            temp_file = zone_dir / "temp"
            if not temp_file.exists():
                continue
            try:
                raw = temp_file.read_bytes()
                if raw is None or not raw.strip():
                    continue
                millideg = int(raw.strip())
            except Exception:  # noqa: BLE001
                continue
            celsius = millideg / 1000.0
            if hottest is None or celsius > hottest[1]:
                hottest = (zone_dir.name, celsius)
        return hottest


class NvmeProbe:
    """NVMe SMART check. DISABLED unless ENABLE_NVME=1 and smartctl is present."""

    def __init__(
        self, config: Config, sender: AlertSender, status: Optional[StatusStore] = None
    ) -> None:
        self.config = config
        self.sender = sender
        self.status = status
        self._faulted = False

    def probe_once(self) -> None:
        # Phase 4 keeps this implemented but inert on the current bench unit.
        # Enable once an NVMe SSD is fitted: set ENABLE_NVME=1 in /etc/kallon/device.env.
        if not self.config.enable_nvme:
            return

        disk_patch: dict[str, Any] = {
            "enabled": True,
            "faulted": self._faulted,
            "device": self.config.nvme_device,
        }
        record_path = os.environ.get("RECORD_PATH", "/var/kallon/recordings")
        try:
            du = shutil.disk_usage(record_path)
            disk_patch.update(
                {
                    "mount": record_path,
                    "space_total_gb": round(du.total / (1024**3), 1),
                    "space_used_gb": round(du.used / (1024**3), 1),
                    "space_free_gb": round(du.free / (1024**3), 1),
                }
            )
        except OSError:
            pass

        smartctl = shutil.which("smartctl")
        if not smartctl:
            LOG.warning("ENABLE_NVME=1 but smartctl is not installed; skipping NVMe check.")
            if self.status is not None:
                self.status.merge("disk", disk_patch)
            return
        try:
            data: dict[str, Any] = {}
            for cmd in (
                [smartctl, "-A", "-j", self.config.nvme_device],
                ["sudo", "-n", smartctl, "-A", "-j", self.config.nvme_device],
            ):
                proc = subprocess.run(  # noqa: S603
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5.0,
                    check=False,
                )
                if proc.returncode not in (0, 4):  # 4 = SMART status flags
                    continue
                parsed = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
                if parsed.get("nvme_smart_health_information_log") or parsed.get("temperature"):
                    data = parsed
                    break
            if not data:
                raise ValueError("smartctl returned no NVMe health data")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("smartctl failed: %s", exc)
            if self.status is not None:
                self.status.merge("disk", disk_patch)
            return
        smart_log = data.get("nvme_smart_health_information_log", {}) or {}
        # Heuristic: reallocated sectors or critical_warning != 0.
        warning = smart_log.get("critical_warning", 0)
        media_errors = smart_log.get("media_errors", 0)
        temp_c = (
            data.get("temperature", {}).get("current")
            or smart_log.get("temperature")
            or 0
        )
        faulted = bool(warning) or media_errors > 0
        if faulted and not self._faulted:
            self._faulted = True
            self.sender.submit(Alert(
                AlertType.DISK_FAULT,
                {
                    "device": self.config.nvme_device,
                    "critical_warning": warning,
                    "media_errors": media_errors,
                    "smart_temp_c": temp_c,
                },
            ))
        disk_patch["faulted"] = self._faulted
        disk_patch["smart_temp_c"] = temp_c
        if smart_log.get("percentage_used") is not None:
            disk_patch["percentage_used"] = smart_log.get("percentage_used")
        if smart_log.get("available_spare") is not None:
            disk_patch["available_spare"] = smart_log.get("available_spare")
        if self.status is not None:
            self.status.merge("disk", disk_patch)


class MotionProbe:
    """Software motion detection via polled accel delta.

    Compares the current accel vector to the previous reading. If any axis
    changes by more than threshold_mg (in milligravities) between consecutive
    polls, fire TAMPER_IMPACT.
    """

    def __init__(
        self,
        config: Config,
        sender: AlertSender,
        mpu: MPU6050,
        status: Optional[StatusStore] = None,
    ) -> None:
        self.config = config
        self.sender = sender
        self.mpu = mpu
        self.status = status
        self._prev: Optional[tuple[float, float, float]] = None

    def probe_once(self) -> None:
        try:
            x, y, z = self.mpu.read_accel_g()
        except Exception:  # noqa: BLE001
            LOG.warning("MPU accel read failed; skipping motion check.")
            return

        if self._prev is not None:
            dx = abs(x - self._prev[0])
            dy = abs(y - self._prev[1])
            dz = abs(z - self._prev[2])
            delta_mg = max(dx, dy, dz) * 1000.0
            threshold = self.config.mpu_accel_threshold_mg
            LOG.info(
                "motion poll now=(%.3f,%.3f,%.3f)g prev=(%.3f,%.3f,%.3f)g "
                "delta_mg=%.1f threshold=%.1f",
                x, y, z, self._prev[0], self._prev[1], self._prev[2],
                delta_mg, threshold,
            )
            if self.status is not None:
                self.status.merge("impact", {"last_delta_mg": round(delta_mg, 1)})
            if delta_mg >= threshold:
                LOG.info(
                    "TAMPER_IMPACT triggered delta_mg=%.1f threshold=%.1f",
                    delta_mg, threshold,
                )
                if self.status is not None:
                    self.status.merge(
                        "impact",
                        {
                            "last_impact_utc": datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            )
                        },
                    )
                self.sender.submit(Alert(
                    AlertType.TAMPER_IMPACT,
                    {
                        "source": "mpu6050",
                        "threshold_mg": threshold,
                        "delta_mg": round(delta_mg, 1),
                        "accel_g": {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3)},
                        "prev_g": {"x": round(self._prev[0], 3), "y": round(self._prev[1], 3), "z": round(self._prev[2], 3)},
                    },
                ))
        else:
            LOG.info("motion poll first reading=(%.3f,%.3f,%.3f)g", x, y, z)
        self._prev = (x, y, z)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Kallon health & tamper watchdog.")
    parser.add_argument("--device-id", default=None, help="Override DEVICE_ID.")
    parser.add_argument("--webhook-url", default=None, help="Override ALERT_WEBHOOK_URL.")
    parser.add_argument("--alert-key-path", default=None, help="Override ALERT_KEY_PATH.")
    parser.add_argument(
        "--rtsp-urls",
        default=None,
        help="Comma-separated RTSP URLs (overrides RTSP_URLS env).",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Initialise everything and exit; useful to verify wiring on the bench.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    config = load_config(args)
    LOG.info(
        "starting kallon_watchdog device_id=%s webhook=%s rtsp_count=%d",
        config.device_id,
        config.webhook_url,
        len(config.rtsp_urls),
    )

    status = StatusStore(config)
    status_server: Optional[StatusServer] = None
    if config.status_api_enable:
        try:
            status_server = StatusServer(
                status, config.status_api_host, config.status_api_port
            )
            status_server.start()
        except Exception as exc:  # noqa: BLE001
            # Never let the optional dashboard surface take down monitoring.
            LOG.error("status API failed to start (continuing without it): %s", exc)
            status_server = None

    sender = AlertSender(config)
    sender.start()

    mpu: Optional[MPU6050] = None
    try:
        mpu = MPU6050(config.mpu_i2c_bus, config.mpu_i2c_addr)
        mpu.init()
        x, y, z = mpu.read_accel_g()
        LOG.info(
            "MPU-6050 ready bus=%d addr=0x%02X baseline=(%.3f, %.3f, %.3f)g threshold=%dmg",
            config.mpu_i2c_bus,
            config.mpu_i2c_addr,
            x, y, z,
            int(config.mpu_accel_threshold_mg),
        )
    except Exception as exc:  # noqa: BLE001
        LOG.error("MPU-6050 init failed; impact alerts disabled: %s", exc)
        mpu = None
    status.update("mpu_present", mpu is not None)

    gpio_handlers = GpioHandlers(config, sender, status)
    try:
        gpio_handlers.setup()
    except Exception:
        LOG.exception("GPIO setup failed")
        sender.stop()
        if status_server is not None:
            status_server.stop()
        return 1

    rtsp_probe = RtspProbe(config, sender, status)
    temp_probe = TemperatureProbe(config, sender, status)
    nvme_probe = NvmeProbe(config, sender, status)
    motion_probe: Optional[MotionProbe] = None
    if mpu is not None:
        motion_probe = MotionProbe(config, sender, mpu, status)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        LOG.info("signal %s received; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.dry_run:
        LOG.info("dry-run complete; exiting before poll loop.")
        gpio_handlers.teardown()
        sender.stop()
        if status_server is not None:
            status_server.stop()
        if mpu is not None:
            mpu.close()
        return 0

    LOG.info("entering poll loop interval=%.1fs", config.poll_interval_sec)
    try:
        while not stop_event.is_set():
            for probe_name, probe_fn in [
                ("rtsp", rtsp_probe.probe_once),
                ("temp", temp_probe.probe_once),
                ("nvme", nvme_probe.probe_once),
                ("motion", motion_probe.probe_once if motion_probe else None),
            ]:
                if probe_fn is None:
                    continue
                try:
                    probe_fn()
                except Exception:  # noqa: BLE001
                    LOG.exception("probe %s crashed; continuing", probe_name)
            stop_event.wait(config.poll_interval_sec)
    finally:
        gpio_handlers.teardown()
        sender.stop()
        if status_server is not None:
            status_server.stop()
        if mpu is not None:
            mpu.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
