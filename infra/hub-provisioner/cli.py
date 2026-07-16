"""kallon-hub-provision — create one hub per customer org (Terra control plane).

Runs on the Terra physical server. Ties the registry, a HubProvider adapter, and
the shared gateway bring-up together:

  1. Ensure the customer exists in the registry (create with --subnet if new);
     set status=pending_hub.
  2. provider.provision() → a reachable Ubuntu host (Option B creates a VM;
     Option C verifies an existing host).
  3. run_gateway_init() copies + runs kallon-gateway-init.sh → manifest
     (also installs tower_proxy with KALLON_HUB_PROXY_TOKEN from Artemis env).
  4. Update the registry (gateway endpoint/pubkey/alert URL/host id, status=active)
     and write gateway_manifest.json (Terra-internal; never sent to the buyer).

Requires ``KALLON_HUB_PROXY_TOKEN`` in the environment / enrollment-api.env when
``KALLON_PROXY_VIA_HUB`` is enabled (default).

Examples:
  python infra/hub-provisioner/cli.py cust_lab --provider manual \
      --host 203.0.113.42 --ssh-user ubuntu --subnet 10.50.0.0/24
  python infra/hub-provisioner/cli.py cust_acme --provider lightsail \
      --region us-east-2 --subnet 10.51.0.0/24
  python infra/hub-provisioner/cli.py cust_lab --provider lightsail \
      --region us-east-2 --instance-name kallon-gateway-lab
  python infra/hub-provisioner/cli.py cust_lab --provider manual --host x --dry-run

Lightsail re-runs adopt an existing hub (by --instance-name, registry hub_host_id,
gateway_endpoint IP, customer tag, or canonical kallon-hub-<id>) before creating.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))     # sibling modules (hyphenated dir)
sys.path.insert(0, str(_ROOT))     # repo-root `registry`

from interface import run_gateway_init  # type: ignore  # noqa: E402
from manual import ManualProvider  # type: ignore  # noqa: E402

from registry import Customer, NotFound, get_registry  # noqa: E402
from registry.identity import slug_of  # noqa: E402


def _provider(name: str, region: str | None):
    name = name.lower()
    if name == "manual":
        return ManualProvider()
    if name == "lightsail":
        from lightsail import LightsailProvider  # type: ignore  # lazy: needs boto3

        return LightsailProvider(region=region or "us-east-1")
    raise SystemExit(f"unknown provider: {name}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="kallon-hub-provision", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("customer_id", help="cust_<slug>")
    p.add_argument("--provider", default="manual", choices=["manual", "lightsail"])
    p.add_argument("--region", default=None)
    p.add_argument("--host", default=None,
                   help="manual: SSH host IP/DNS; lightsail: optional instance name")
    p.add_argument("--instance-name", default=None,
                   help="Lightsail instance name to adopt (e.g. kallon-gateway-lab)")
    p.add_argument("--ssh-user", default="ubuntu")
    p.add_argument("--subnet", default=None, help="create customer with this /24 if new")
    p.add_argument("--display-name", default=None)
    p.add_argument("--public-endpoint", default=None)
    p.add_argument("--manifest-dir", default=str(_ROOT / "manifests"))
    p.add_argument("--registry", choices=["postgres", "sqlite"], default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    reg = get_registry(args.registry)
    try:
        # 1. customer row
        try:
            cust = reg.get_customer(args.customer_id)
        except NotFound:
            if not args.subnet:
                raise SystemExit(f"{args.customer_id} not found; pass --subnet to create it")
            cust = reg.create_customer(Customer(
                customer_id=args.customer_id,
                display_name=args.display_name or slug_of(args.customer_id),
                vpn_subnet=args.subnet,
                hub_provider=args.provider,
            ))
            reg.audit("customer_created", entity_id=args.customer_id, actor="hub-provision")
        reg.update_customer_hub(args.customer_id, status="pending_hub", hub_provider=args.provider)
        if args.display_name:
            reg.update_customer_hub(args.customer_id, display_name=args.display_name)

        # 2. provision host (Lightsail adopts existing hub when registry/IP/name match)
        prov = _provider(args.provider, args.region)
        host = prov.provision(
            args.customer_id,
            host=args.host,
            instance_name=args.instance_name,
            hub_host_id=cust.hub_host_id,
            gateway_endpoint=cust.gateway_endpoint,
            ssh_user=args.ssh_user,
            region=args.region,
            dry_run=args.dry_run,
        )
        print(f"[provision] host={host.public_ip} id={host.host_id} provider={host.provider}",
              file=sys.stderr)

        # 3. gateway bring-up
        manifest = run_gateway_init(
            host, customer_id=args.customer_id, vpn_subnet=cust.vpn_subnet,
            public_endpoint=args.public_endpoint or (None if not args.dry_run else host.public_ip),
            dry_run=args.dry_run,
        )

        # 4. persist
        if not args.dry_run:
            reg.update_customer_hub(
                args.customer_id,
                gateway_endpoint=manifest["gateway_endpoint"],
                gateway_public_key=manifest["gateway_public_key"],
                hub_alert_url=manifest["alert_webhook_url"],
                hub_provider=args.provider,
                hub_host_id=host.host_id,
                status="active",
            )
            reg.audit("customer_hub_active", entity_id=args.customer_id, actor="hub-provision",
                      payload_json={"host_id": host.host_id})
            os.makedirs(args.manifest_dir, exist_ok=True)
            mpath = Path(args.manifest_dir) / f"gateway_manifest_{args.customer_id}.json"
            mpath.write_text(json.dumps(manifest, indent=2))
            print(f"[manifest] wrote {mpath}", file=sys.stderr)

        print(json.dumps(manifest, indent=2))
        return 0
    finally:
        reg.close()


if __name__ == "__main__":
    raise SystemExit(main())
