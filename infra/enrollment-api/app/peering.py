"""Hub peer-add hook for the enrollment API.

When a tower enrolls, its WireGuard public key must become a peer on the
customer hub. This module isolates *how* that happens so the API stays testable:

  * subprocess (production): runs scripts/kallon-gateway-add-peer.sh against the
    customer's gateway host (set via KALLON_ADDPEER_CMD). Required on Path P.
  * noop (lab/tests only): record-only; operator adds peers manually.

Selected by KALLON_PEER_BACKEND = subprocess | noop (defaults to noop if unset —
production must set subprocess explicitly).
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Protocol

log = logging.getLogger("enrollment.peering")


class PeerAdder(Protocol):
    def add_peer(self, *, gateway_host: str, pubkey: str, vpn_ip: str, device_id: str) -> None: ...


class NoopPeerAdder:
    """Records the intent only — a human / Option C runbook adds the peer."""

    def add_peer(self, *, gateway_host: str, pubkey: str, vpn_ip: str, device_id: str) -> None:
        log.warning(
            "NOOP peer add: device=%s vpn_ip=%s host=%s (add manually via "
            "kallon-gateway-add-peer.sh)", device_id, vpn_ip, gateway_host,
        )


class SubprocessPeerAdder:
    """Invokes kallon-gateway-add-peer.sh. The script itself is idempotent."""

    def __init__(self, cmd_template: str) -> None:
        # Template supports {gateway_host} {pubkey} {vpn_ip} {device_id}.
        self._tpl = cmd_template

    def add_peer(self, *, gateway_host: str, pubkey: str, vpn_ip: str, device_id: str) -> None:
        cmd = self._tpl.format(
            gateway_host=shlex.quote(gateway_host),
            pubkey=shlex.quote(pubkey),
            vpn_ip=shlex.quote(f"{vpn_ip}/32"),
            device_id=shlex.quote(device_id),
        )
        log.info("add_peer: %s", cmd)
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"add-peer failed ({res.returncode}): {res.stderr.strip()}")


def get_peer_adder() -> PeerAdder:
    backend = os.environ.get("KALLON_PEER_BACKEND", "noop").lower()
    if backend == "subprocess":
        tpl = os.environ.get(
            "KALLON_ADDPEER_CMD",
            "scripts/kallon-gateway-add-peer.sh --gateway-host {gateway_host} "
            "--pubkey {pubkey} --vpn-ip {vpn_ip} --device-id {device_id}",
        )
        return SubprocessPeerAdder(tpl)
    return NoopPeerAdder()
