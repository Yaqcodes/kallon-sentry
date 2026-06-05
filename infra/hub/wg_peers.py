#!/usr/bin/env python3
"""Canonical WireGuard peer persistence for the customer hub.

Single source of truth for how a tower peer is written into wg0.conf
(idempotent: re-adding the same public key replaces its block, never
duplicates). Used by tests; kallon-gateway-add-peer.sh applies the identical
algorithm on the hub (plus a live `wg set`).

CLI:  python3 wg_peers.py <conf_path> <pubkey> <vpn_ip/32> <device_id>
"""
from __future__ import annotations

import re
import sys


def add_or_replace_peer(conf_text: str, pubkey: str, vpn_ip: str, device_id: str) -> str:
    """Return conf_text with the peer for `pubkey` added or replaced."""
    if "/" not in vpn_ip:
        vpn_ip = f"{vpn_ip}/32"
    blocks = re.split(r"(?m)^\[Peer\]\s*$", conf_text)
    head = blocks[0]
    kept = []
    for b in blocks[1:]:
        norm = b.replace(" ", "")
        # Drop an existing block matching this key OR this device (handles key
        # rotation: the same device re-enrolling with a new key replaces, not
        # duplicates).
        if f"PublicKey={pubkey}" in norm or f"#{device_id}" in norm:
            continue
        kept.append(b)
    out = head.rstrip() + "\n"
    for b in kept:
        out += "[Peer]" + b.rstrip() + "\n"
    out += f"\n[Peer]\n# {device_id}\nPublicKey = {pubkey}\nAllowedIPs = {vpn_ip}\n"
    return out


def count_peers(conf_text: str) -> int:
    return len(re.findall(r"(?m)^\[Peer\]\s*$", conf_text))


def _main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: wg_peers.py <conf> <pubkey> <vpn_ip> <device_id>", file=sys.stderr)
        return 2
    conf, pubkey, vpn_ip, device_id = argv[1:5]
    try:
        with open(conf, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = "[Interface]\n"
    new = add_or_replace_peer(text, pubkey, vpn_ip, device_id)
    with open(conf, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"persisted peer {device_id} ({pubkey[:12]}...) -> {vpn_ip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
