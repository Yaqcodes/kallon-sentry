"""Render factory /etc/kallon/device.env from order parameters."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEVICE_ENV_EXAMPLE = REPO_ROOT / "deploy" / "device.env.example"

# Standard factory camera VLAN addressing (one /32 per camera on the tower).
FACTORY_CAMERA_PREFIX = "192.168.10"
FACTORY_CAMERA_BASE_OCTET = 108


def factory_camera_ips(count: int) -> str:
    if count < 1:
        raise ValueError("cameras must be >= 1")
    return ",".join(
        f"{FACTORY_CAMERA_PREFIX}.{FACTORY_CAMERA_BASE_OCTET + i}" for i in range(count)
    )


def factory_rtsp_urls(count: int) -> str:
    return ",".join(f"rtsp://127.0.0.1:8554/cam{i}" for i in range(1, count + 1))


def render_device_env(
    *,
    device_id: str,
    customer_id: str,
    claim_code: str,
    enrollment_token: str,
    enrollment_url: str,
    cameras: int,
    camera_password: str = "REPLACE_ME",
) -> str:
    """Build device.env content for factory bake-in (VPN fields filled at enroll)."""
    camera_ips = factory_camera_ips(cameras)
    rtsp_urls = factory_rtsp_urls(cameras)

    lines = [
        "# Rendered by kallon-fulfill-order - do not commit.",
        f"DEVICE_ID={device_id}",
        f"CUSTOMER_ID={customer_id}",
        f"CLAIM_CODE={claim_code}",
        "",
        "WAN_MODE=wifi",
        "WAN_IFACE=wlP1p1s0",
        "WAN_FALLBACK_IFACE=usb0",
        "WAN_METRIC=100",
        "WAN_FALLBACK_METRIC=700",
        "",
        "CAMERA_IFACE=enP8p1s0",
        "CAMERA_SUBNET=192.168.10.0/24",
        "CAMERA_JETSON_IP=192.168.10.2/24",
        f"CAMERA_IPS={camera_ips}",
        "CAMERA_RTSP_USER=admin",
        f"CAMERA_PASSWORD={camera_password}",
        "CAMERA_RTSP_PATH=/cam/realmonitor?channel=1&subtype=1",
        "",
        "WG_IFACE=wg0",
        "WG_PRIVATE_KEY_PATH=/etc/wireguard/jetson.private",
        "VPN_IP=10.50.0.2/32",
        "GATEWAY_ENDPOINT=REPLACE_AT_ENROLL",
        "GATEWAY_PUBLIC_KEY=REPLACE_AT_ENROLL",
        "VPN_SUBNET=10.50.0.0/24",
        "",
        f"ENROLLMENT_URL={enrollment_url}",
        f"ENROLLMENT_TOKEN={enrollment_token}",
        "",
        "ALERT_WEBHOOK_URL=http://10.50.0.1:8080/alerts",
        "ALERT_KEY_PATH=/etc/kallon/alert.key",
        f"RTSP_URLS={rtsp_urls}",
        "",
        "POLL_INTERVAL_SEC=10",
        "TEMP_TRIGGER_C=80",
        "TEMP_CLEAR_C=75",
        "DEDUP_WINDOW_SEC=60",
        "",
        "MPU_I2C_BUS=7",
        "MPU_I2C_ADDR=0x68",
        "GPIO_REED_PIN=31",
        "GPIO_LDR_PIN=33",
        "MPU_ACCEL_THRESHOLD_MG=150",
        "",
        "ENABLE_NVME=0",
        "NVME_DEVICE=/dev/nvme0",
        "ENABLE_POWER_ADC=0",
    ]
    return "\n".join(lines) + "\n"


def write_factory_file(path: Path, content: str) -> None:
    """Write text for Linux factory/Jetson consumption — always LF, never CRLF.

    pathlib.Path.write_text() defaults to os.linesep; on Windows that produces
    \\r\\n and breaks `source device.env` on the tower.
    """
    path.write_text(content, encoding="utf-8", newline="\n")
