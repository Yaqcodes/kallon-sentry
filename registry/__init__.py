"""Kallon fleet registry package.

Production store is Postgres (KALLON_REGISTRY=postgres, default). The SQLite
provider exists for unit tests only.
"""
from __future__ import annotations

import os

from .interface import (
    Conflict,
    Customer,
    NotFound,
    RegistryError,
    RegistryProvider,
    SubnetExhausted,
    Tower,
)

__all__ = [
    "Customer",
    "Tower",
    "RegistryProvider",
    "RegistryError",
    "NotFound",
    "Conflict",
    "SubnetExhausted",
    "get_registry",
]


def get_registry(kind: str | None = None) -> RegistryProvider:
    """Return a registry provider.

    kind: 'postgres' (default/production) or 'sqlite' (tests). Falls back to the
    KALLON_REGISTRY env var, then 'postgres'.
    """
    kind = (kind or os.environ.get("KALLON_REGISTRY") or "postgres").lower()
    if kind == "postgres":
        from .postgres_provider import PostgresRegistry

        return PostgresRegistry()
    if kind == "sqlite":
        from .sqlite_provider import SQLiteRegistry

        path = os.environ.get("KALLON_SQLITE_PATH", ":memory:")
        reg = SQLiteRegistry(path)
        reg.init_schema()
        return reg
    raise ValueError(f"unknown registry kind: {kind}")
