"""Unit tests for tower-dashboard recording helpers (no live MediaMTX)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[1] / "infra" / "tower-dashboard" / "gateway.py"


def _load_gateway():
    spec = importlib.util.spec_from_file_location("tower_gateway", GATEWAY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Avoid binding a real HTTP server on import — gateway only binds in main().
    sys.modules["tower_gateway"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gw():
    return _load_gateway()


def test_record_patch_body_off(gw):
    body = gw._record_patch_body(False)
    assert body == {"record": False}
    assert "sourceOnDemand" not in body


def test_record_patch_body_on(gw, monkeypatch):
    monkeypatch.setenv("RECORD_PATH", "/var/kallon/recordings")
    monkeypatch.setenv("RECORD_MEDIAMTX_DELETE_AFTER", "48")
    monkeypatch.setenv("RECORD_MEDIAMTX_SEGMENT_FILE_DURATION", "30m")
    # Re-read module-level RECORD_PATH? It was captured at import — patch attribute.
    monkeypatch.setattr(gw, "RECORD_PATH", "/var/kallon/recordings")
    body = gw._record_patch_body(True)
    assert body["record"] is True
    assert body["sourceOnDemand"] is False
    assert body["recordDeleteAfter"] == "48h"
    assert body["recordSegmentDuration"] == "30m"
    assert body["recordPath"].startswith("/var/kallon/recordings/")


def test_env_record_enable(gw, tmp_path, monkeypatch):
    env = tmp_path / "device.env"
    env.write_text("# comment\nRECORD_ENABLE=1\nDEVICE_ID=kln_lab_000001\n", encoding="utf-8")
    monkeypatch.setattr(gw, "DEVICE_ENV_PATH", env)
    assert gw._env_record_enable() is True
    env.write_text("RECORD_ENABLE=0\n", encoding="utf-8")
    assert gw._env_record_enable() is False
