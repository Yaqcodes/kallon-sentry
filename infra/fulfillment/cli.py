"""kallon-fulfill-order — internal order fulfillment for Terra ops.

One command per customer order:
  1. Ensure cust_<slug> exists (auto-assign /24 if new)
  2. Hub-provision if hub is not active
  3. Register N towers and render device.env per unit

Examples:
  python infra/fulfillment/cli.py acme --display-name "Acme Security" --towers 3 --cameras 2
  python infra/fulfillment/cli.py lab --provider manual --host 18.220.75.237 \\
      --towers 1 --cameras 2 --output-dir C:\\kallon\\factory\\lab
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_ROOT))

from registry import Customer, NotFound, get_registry  # noqa: E402
from registry.identity import (  # noqa: E402
    customer_id as mk_customer_id,
    device_id as mk_device_id,
    gateway_id as mk_gateway_id,
    new_claim_code,
    new_enrollment_token,
)
from registry.subnet import next_customer_subnet  # noqa: E402

from .device_env import render_device_env, write_factory_file  # noqa: E402

HUB_CLI = _ROOT / "infra" / "hub-provisioner" / "cli.py"
_SLUG_RE = re.compile(r"^[a-z0-9]+$")


def _normalize_slug(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", raw.lower())
    if not slug or not _SLUG_RE.match(slug):
        raise SystemExit(f"invalid slug {raw!r} — use lowercase letters and digits only")
    return slug


def _next_serial(reg, slug: str, customer_id: str) -> int:
    prefix = f"kln_{slug}_"
    highest = 0
    for tower in reg.list_towers(customer_id):
        if tower.device_id.startswith(prefix):
            tail = tower.device_id[len(prefix):]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return highest + 1


def _run_hub_provision(
    customer_id: str,
    *,
    provider: str,
    subnet: str,
    display_name: str,
    host: str | None,
    region: str | None,
    ssh_user: str,
    dry_run: bool,
) -> None:
    cmd = [
        sys.executable, str(HUB_CLI), customer_id,
        "--provider", provider,
        "--subnet", subnet,
        "--display-name", display_name,
        "--ssh-user", ssh_user,
    ]
    if host:
        cmd.extend(["--host", host])
    if region:
        cmd.extend(["--region", region])
    if dry_run:
        cmd.append("--dry-run")
    subprocess.run(cmd, check=True, env=os.environ)


def fulfill_order(
    slug: str,
    *,
    display_name: str,
    towers: int,
    cameras: int,
    provider: str = "lightsail",
    host: str | None = None,
    region: str | None = None,
    ssh_user: str = "ubuntu",
    subnet: str | None = None,
    enrollment_url: str | None = None,
    camera_password: str = "REPLACE_ME",
    output_dir: Path | None = None,
    registry_name: str | None = None,
    dry_run: bool = False,
) -> dict:
    slug = _normalize_slug(slug)
    cid = mk_customer_id(slug)
    enrollment_url = (enrollment_url or os.environ.get("KALLON_ENROLLMENT_URL", "")).strip()
    if not enrollment_url and not dry_run:
        raise SystemExit("set KALLON_ENROLLMENT_URL or pass --enrollment-url")

    out_dir = output_dir or (_ROOT / "fulfillment-output" / slug)
    plan: dict = {
        "customer_id": cid,
        "slug": slug,
        "towers": towers,
        "cameras": cameras,
        "output_dir": str(out_dir),
        "units": [],
    }

    reg = get_registry(registry_name)
    try:
        cust = None
        try:
            cust = reg.get_customer(cid)
            plan["subnet"] = cust.vpn_subnet
            plan["customer_existed"] = True
            if display_name:
                if not dry_run:
                    reg.update_customer_hub(cid, display_name=display_name)
                plan["display_name_updated"] = display_name
        except NotFound:
            plan["customer_existed"] = False
            assigned = subnet or next_customer_subnet([c.vpn_subnet for c in reg.list_customers()])
            plan["subnet"] = assigned
            if not dry_run:
                reg.create_customer(Customer(
                    customer_id=cid,
                    display_name=display_name,
                    vpn_subnet=assigned,
                    gateway_id=mk_gateway_id(slug),
                    hub_provider=provider,
                ))
                reg.audit("customer_created", entity_id=cid, actor="fulfill-order",
                          payload_json={"subnet": assigned})
            else:
                cust = None

        if cust is None and not dry_run:
            cust = reg.get_customer(cid)
        needs_hub = cust is None or cust.status != "active" or not cust.gateway_endpoint
        plan["hub_provision_needed"] = needs_hub
        if needs_hub:
            _run_hub_provision(
                cid,
                provider=provider,
                subnet=plan["subnet"],
                display_name=display_name,
                host=host,
                region=region,
                ssh_user=ssh_user,
                dry_run=dry_run,
            )

        serial = _next_serial(reg, slug, cid)
        for i in range(towers):
            serial_num = serial + i
            did = mk_device_id(slug, serial_num)
            token = new_enrollment_token()
            claim = new_claim_code()
            unit = {
                "device_id": did,
                "serial": serial_num,
                "claim_code": claim,
                "enrollment_token": token,
                "device_env": str(out_dir / f"device_{did}.env"),
            }
            if dry_run:
                plan["units"].append(unit)
                continue

            from registry.interface import Tower  # lazy import

            reg.register_tower(Tower(
                device_id=did,
                customer_id=cid,
                claim_code=claim,
                enrollment_token_hash=hashlib.sha256(token.encode()).hexdigest(),
            ))
            reg.audit("tower_registered", entity_id=did, actor="fulfill-order")

            out_dir.mkdir(parents=True, exist_ok=True)
            env_path = out_dir / f"device_{did}.env"
            write_factory_file(env_path, render_device_env(
                device_id=did,
                customer_id=cid,
                claim_code=claim,
                enrollment_token=token,
                enrollment_url=enrollment_url,
                cameras=cameras,
                camera_password=camera_password,
            ))
            unit["qr_payload"] = f"kallon://claim/{claim}"
            plan["units"].append(unit)

        manifest_path = out_dir / f"fulfillment_{cid}.json"
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            write_factory_file(manifest_path, json.dumps(plan, indent=2))
        plan["manifest"] = str(manifest_path)
        return plan
    finally:
        reg.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="kallon-fulfill-order",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("slug", help="customer slug (e.g. acme → cust_acme)")
    p.add_argument("--display-name", required=True, help="human-readable customer name")
    p.add_argument("--towers", type=int, default=1, help="number of towers to register")
    p.add_argument("--cameras", type=int, default=1, help="cameras per tower (factory CAMERA_IPS)")
    p.add_argument("--provider", default="lightsail", choices=["manual", "lightsail"])
    p.add_argument("--host", default=None, help="existing hub host (manual provider)")
    p.add_argument("--region", default=None)
    p.add_argument("--ssh-user", default="ubuntu")
    p.add_argument("--subnet", default=None, help="override auto /24 assignment for new customers")
    p.add_argument("--enrollment-url", default=None, help="default: KALLON_ENROLLMENT_URL env")
    p.add_argument("--camera-password", default="REPLACE_ME")
    p.add_argument("--output-dir", default=None, help="where to write device_*.env files")
    p.add_argument("--registry", choices=["postgres", "sqlite"], default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.towers < 1:
        raise SystemExit("--towers must be >= 1")
    if args.cameras < 1:
        raise SystemExit("--cameras must be >= 1")

    out = fulfill_order(
        args.slug,
        display_name=args.display_name,
        towers=args.towers,
        cameras=args.cameras,
        provider=args.provider,
        host=args.host,
        region=args.region,
        ssh_user=args.ssh_user,
        subnet=args.subnet,
        enrollment_url=args.enrollment_url,
        camera_password=args.camera_password,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        registry_name=args.registry,
        dry_run=args.dry_run,
    )
    print(json.dumps(out, indent=2))
    if not args.dry_run:
        print(
            f"\nWrote {len(out['units'])} device.env file(s) under {out['output_dir']}. "
            "Copy ENROLLMENT_TOKEN to factory once; tokens are in fulfillment JSON.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
