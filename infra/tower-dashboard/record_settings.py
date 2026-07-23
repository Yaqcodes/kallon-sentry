"""Resolve continuous-recording settings from device.env (single source of truth).

All callers (gateway, apply-recording, installer) must use these helpers so
MediaMTX never silently diverges from /etc/kallon/device.env.

Defaults below match deploy/device.env.example and are used ONLY when a key
is absent from the env file / process environment.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

# Fallbacks when keys are missing from device.env — keep in sync with
# deploy/device.env.example (do not scatter alternate defaults elsewhere).
DEFAULT_SEGMENT_DURATION = "15m"
DEFAULT_DELETE_AFTER = "168h"
DEFAULT_RECORD_PATH = "/var/kallon/recordings"


def load_device_env(path: str | Path) -> dict[str, str]:
    """Parse KEY=VAL lines from device.env. Missing file → empty dict."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return {}
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    out: dict[str, str] = {}
    for line_raw in text.splitlines():
        line = line_raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip("\"'")
    return out


def _truthy(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _first(*vals: Optional[str]) -> Optional[str]:
    for v in vals:
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def _normalize_delete_after(raw: str) -> str:
    raw = raw.strip()
    if re.fullmatch(r"[0-9]+", raw):
        return f"{raw}h"
    return raw


def resolve_record_settings(
    env: Optional[Mapping[str, str]] = None,
    *,
    device_env_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return recording settings resolved from device.env (+ optional process env).

    Priority for each key: device.env file → process os.environ → default.

    Local retention (RECORD_MEDIAMTX_DELETE_AFTER) is always applied to MediaMTX
    as recordDeleteAfter — age-delete runs whether or not a segment was uploaded.
    The uploader must NOT delete locals immediately after upload; MediaMTX
    removes them once they exceed retention.
    """
    file_env: dict[str, str] = {}
    if device_env_path is not None:
        file_env = load_device_env(device_env_path)

    def get(key: str, *aliases: str) -> Optional[str]:
        for k in (key, *aliases):
            if k in file_env and file_env[k] != "":
                return file_env[k]
        if env is not None:
            for k in (key, *aliases):
                if k in env and str(env[k]).strip() != "":
                    return str(env[k]).strip()
        for k in (key, *aliases):
            v = os.environ.get(k)
            if v is not None and v.strip() != "":
                return v.strip()
        return None

    segment = _first(
        get("RECORD_MEDIAMTX_SEGMENT_FILE_DURATION", "RECORD_SEGMENT_DURATION"),
    ) or DEFAULT_SEGMENT_DURATION

    configured_delete = _normalize_delete_after(
        _first(get("RECORD_MEDIAMTX_DELETE_AFTER", "RECORD_RETENTION"))
        or DEFAULT_DELETE_AFTER
    )

    record_path = _first(get("RECORD_PATH")) or DEFAULT_RECORD_PATH
    upload_enable = _truthy(get("RECORD_UPLOAD_ENABLE"))
    record_enable = _truthy(get("RECORD_ENABLE"))

    return {
        "record_path": record_path.rstrip("/"),
        "segment_duration": segment,
        "delete_after_configured": configured_delete,
        "delete_after_effective": configured_delete,
        "upload_enable": upload_enable,
        "record_enable": record_enable,
    }
