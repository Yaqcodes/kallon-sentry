"""Kallon hub provisioner — one hub VM per customer org, via pluggable adapters."""
from __future__ import annotations

from .interface import HubHost, HubProvider, run_gateway_init

__all__ = ["HubHost", "HubProvider", "run_gateway_init", "get_provider"]


def get_provider(name: str, **kwargs) -> HubProvider:
    name = name.lower()
    if name == "lightsail":
        from .lightsail import LightsailProvider

        return LightsailProvider(**{k: v for k, v in kwargs.items()
                                    if k in ("blueprint", "bundle", "region")})
    if name == "manual":
        from .manual import ManualProvider

        return ManualProvider()
    raise ValueError(f"unknown hub provider: {name} (have: lightsail, manual)")
