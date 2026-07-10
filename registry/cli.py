"""kallon-registry CLI — the only sanctioned way to mutate the registry by hand.

Factory scripts and the enrollment API import the providers directly; humans use
this CLI. Never write raw SQL in install scripts.

Examples:
  python -m registry.cli init-schema
  python -m registry.cli create-customer --slug acme --name "Acme Security" \
      --subnet 10.50.0.0/24 --provider lightsail
  python -m registry.cli register-tower --slug acme --serial 42
  python -m registry.cli allocate-ip --customer cust_acme
  python -m registry.cli get-config --device kln_acme_000042
  python -m registry.cli list-towers --customer cust_acme
  python -m registry.cli set-tower-status --device kln_acme_000042 --status suspended
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

from . import get_registry
from .identity import (
    customer_id as mk_customer_id,
    device_id as mk_device_id,
    gateway_id as mk_gateway_id,
    new_claim_code,
    new_enrollment_token,
)
from .interface import Customer, RegistryError, Tower


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_init_schema(reg, args) -> int:
    reg.init_schema()
    _out({"ok": True, "action": "init-schema"})
    return 0


def cmd_create_customer(reg, args) -> int:
    cid = mk_customer_id(args.slug)
    cust = Customer(
        customer_id=cid,
        display_name=args.name,
        vpn_subnet=args.subnet,
        gateway_id=mk_gateway_id(args.slug),
        hub_provider=args.provider,
    )
    created = reg.create_customer(cust)
    reg.audit("customer_created", entity_id=cid, actor=args.actor, payload_json={"subnet": args.subnet})
    _out(created.to_dict())
    return 0


def cmd_register_tower(reg, args) -> int:
    cid = mk_customer_id(args.slug)
    did = mk_device_id(args.slug, args.serial)
    claim = new_claim_code()
    token = new_enrollment_token()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    tower = Tower(
        device_id=did,
        customer_id=cid,
        group_id=args.group,
        claim_code=claim,
        enrollment_token_hash=token_hash,
    )
    created = reg.register_tower(tower)
    reg.audit("tower_registered", entity_id=did, actor=args.actor)
    result = created.to_dict()
    # The plaintext token + claim are returned ONCE for factory bake-in; never stored plaintext.
    result["_enrollment_token_PLAINTEXT"] = token
    result["_claim_code"] = claim
    _out(result)
    return 0


def cmd_allocate_ip(reg, args) -> int:
    ip = reg.allocate_ip(args.customer)
    reg.audit("ip_allocated", entity_id=args.customer, actor=args.actor, payload_json={"vpn_ip": ip})
    _out({"customer_id": args.customer, "vpn_ip": ip})
    return 0


def cmd_set_hub(reg, args) -> int:
    cust = reg.update_customer_hub(
        args.customer,
        gateway_endpoint=args.endpoint,
        gateway_public_key=args.pubkey,
        hub_alert_url=args.alert_url,
        hub_provider=args.provider,
        hub_host_id=args.host_id,
        status=args.status,
    )
    reg.audit("customer_hub_updated", entity_id=args.customer, actor=args.actor)
    _out(cust.to_dict())
    return 0


def cmd_get_config(reg, args) -> int:
    tower = reg.get_tower(args.device)
    cust = reg.get_customer(tower.customer_id)
    _out({
        "device_id": tower.device_id,
        "customer_id": cust.customer_id,
        "vpn_ip": tower.vpn_ip,
        "vpn_subnet": cust.vpn_subnet,
        "gateway_endpoint": cust.gateway_endpoint,
        "gateway_public_key": cust.gateway_public_key,
        "hub_alert_url": cust.hub_alert_url,
        "status": tower.status,
    })
    return 0


def cmd_list_customers(reg, args) -> int:
    _out([c.to_dict() for c in reg.list_customers()])
    return 0


def cmd_list_towers(reg, args) -> int:
    _out([t.to_dict() for t in reg.list_towers(args.customer)])
    return 0


def cmd_set_tower_status(reg, args) -> int:
    # suspended: the enrollment API rejects /v1/enroll for this device_id
    # (403) instead of touching the hub — use this to pull a misbehaving or
    # decommissioned tower out of rotation without deleting its registration.
    tower = reg.set_tower_status(args.device, args.status)
    reg.audit("tower_status_set", entity_id=args.device, actor=args.actor,
              payload_json={"status": args.status})
    _out(tower.to_dict())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kallon-registry", description=__doc__)
    p.add_argument("--registry", choices=["postgres", "sqlite"], default=None,
                   help="override KALLON_REGISTRY (default postgres)")
    p.add_argument("--actor", default="cli", help="audit actor label")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-schema").set_defaults(func=cmd_init_schema)

    s = sub.add_parser("create-customer")
    s.add_argument("--slug", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--subnet", required=True, help="e.g. 10.50.0.0/24")
    s.add_argument("--provider", default="manual",
                   choices=["lightsail", "hetzner", "ovh", "proxmox", "manual"])
    s.set_defaults(func=cmd_create_customer)

    s = sub.add_parser("register-tower")
    s.add_argument("--slug", required=True)
    s.add_argument("--serial", type=int, required=True)
    s.add_argument("--group", default=None)
    s.set_defaults(func=cmd_register_tower)

    s = sub.add_parser("allocate-ip")
    s.add_argument("--customer", required=True)
    s.set_defaults(func=cmd_allocate_ip)

    s = sub.add_parser("set-hub")
    s.add_argument("--customer", required=True)
    s.add_argument("--endpoint")
    s.add_argument("--pubkey")
    s.add_argument("--alert-url")
    s.add_argument("--provider", choices=["lightsail", "hetzner", "ovh", "proxmox", "manual"])
    s.add_argument("--host-id")
    s.add_argument("--status", choices=["pending_hub", "active", "suspended"])
    s.set_defaults(func=cmd_set_hub)

    s = sub.add_parser("get-config")
    s.add_argument("--device", required=True)
    s.set_defaults(func=cmd_get_config)

    sub.add_parser("list-customers").set_defaults(func=cmd_list_customers)

    s = sub.add_parser("list-towers")
    s.add_argument("--customer", default=None)
    s.set_defaults(func=cmd_list_towers)

    s = sub.add_parser("set-tower-status",
                        help="e.g. suspend a tower to pull it out of rotation without deleting it")
    s.add_argument("--device", required=True)
    s.add_argument("--status", required=True,
                    choices=["manufactured", "enrolled", "active", "suspended"])
    s.set_defaults(func=cmd_set_tower_status)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    reg = get_registry(args.registry)
    try:
        return args.func(reg, args)
    except RegistryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        reg.close()


if __name__ == "__main__":
    raise SystemExit(main())
