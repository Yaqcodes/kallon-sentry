"""Customer VPN /24 allocation for the Terra fleet registry."""
from __future__ import annotations

import ipaddress

from .interface import SubnetExhausted

# cust_lab → 10.50.0.0/24, cust_acme → 10.51.0.0/24, …
DEFAULT_BASE_SECOND_OCTET = 50


def next_customer_subnet(
    existing_subnets: list[str],
    *,
    base_second_octet: int = DEFAULT_BASE_SECOND_OCTET,
) -> str:
    """Return the next free 10.x.0.0/24 not already assigned to a customer."""
    used: set[int] = set()
    for raw in existing_subnets:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            continue
        if net.version != 4 or net.prefixlen != 24:
            continue
        octets = net.network_address.packed
        if octets[0] != 10:
            continue
        used.add(octets[1])

    for second in range(base_second_octet, 256):
        if second not in used:
            return f"10.{second}.0.0/24"
    raise SubnetExhausted("no free customer /24 left in 10.x.0.0 plan")
