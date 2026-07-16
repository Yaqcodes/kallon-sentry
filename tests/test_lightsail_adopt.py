"""Unit tests for Lightsail hub adoption / anti-duplicate resolution."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "infra" / "hub-provisioner"))

from lightsail import LightsailProvider  # noqa: E402


def _inst(name: str, ip: str | None = None, customer: str | None = None) -> dict:
    tags = [{"key": "kallon-customer", "value": customer}] if customer else []
    return {"name": name, "publicIpAddress": ip, "tags": tags}


@pytest.fixture
def provider() -> LightsailProvider:
    return LightsailProvider(region="us-east-2")


def test_adopt_by_explicit_instance_name(provider: LightsailProvider) -> None:
    ls = MagicMock()
    ls.get_instances.return_value = {
        "instances": [_inst("kallon-gateway-lab", "18.220.75.237")],
    }
    name, create = provider._resolve_instance_name(
        ls, "cust_lab", instance_name="kallon-gateway-lab",
    )
    assert name == "kallon-gateway-lab" and create is False


def test_adopt_by_gateway_endpoint_ip(provider: LightsailProvider) -> None:
    ls = MagicMock()
    ls.get_instances.return_value = {
        "instances": [
            _inst("kallon-hub-cust_lab", "18.223.213.147"),
            _inst("kallon-gateway-lab", "18.220.75.237"),
        ],
    }
    name, create = provider._resolve_instance_name(
        ls, "cust_lab", gateway_endpoint="18.220.75.237:51820",
    )
    assert name == "kallon-gateway-lab" and create is False


def test_refuse_create_when_endpoint_ip_orphaned(provider: LightsailProvider) -> None:
    ls = MagicMock()
    ls.get_instances.return_value = {
        "instances": [_inst("kallon-hub-cust_lab", "18.223.213.147")],
    }
    with pytest.raises(RuntimeError, match="Refusing to create a second hub"):
        provider._resolve_instance_name(
            ls, "cust_lab", gateway_endpoint="18.220.75.237:51820",
        )


def test_hub_host_id_ip_does_not_block_ip_match(provider: LightsailProvider) -> None:
    """Legacy registry stored the public IP as hub_host_id — still adopt via endpoint."""
    ls = MagicMock()
    ls.get_instances.return_value = {
        "instances": [_inst("kallon-gateway-lab", "18.220.75.237")],
    }
    name, create = provider._resolve_instance_name(
        ls,
        "cust_lab",
        hub_host_id="18.220.75.237",
        gateway_endpoint="18.220.75.237:51820",
    )
    assert name == "kallon-gateway-lab" and create is False


def test_greenfield_creates_canonical(provider: LightsailProvider) -> None:
    ls = MagicMock()
    ls.get_instances.return_value = {"instances": []}
    name, create = provider._resolve_instance_name(ls, "cust_acme")
    assert name == "kallon-hub-cust_acme" and create is True
