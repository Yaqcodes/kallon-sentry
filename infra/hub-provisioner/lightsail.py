"""AWS Lightsail HubProvider (default Option B adapter).

Creates a small Ubuntu Lightsail instance, opens UDP 51820 (WireGuard),
TCP 8767 (hub tower-proxy for Artemis), and TCP 8768 (hub HLS for live video),
waits for SSH, and returns its static/public IP. Bring-up is the shared
run_gateway_init().

Idempotency (re-run safe):
  1. Explicit ``instance_name`` / CLI ``--instance-name``
  2. Registry ``hub_host_id`` when it is a Lightsail instance name (not an IP)
  3. Instance whose public IP matches registry ``gateway_endpoint``
  4. Instance tagged ``kallon-customer=<customer_id>``
  5. Canonical name ``kallon-hub-<customer_id>`` if it already exists
  6. Only then create ``kallon-hub-<customer_id>``

If the registry already has a gateway IP but no Lightsail instance owns it,
we refuse to create a second hub (avoids the kallon-gateway-lab vs
kallon-hub-cust_* split).

Requires boto3 + AWS credentials (e.g. an ops IAM user with Lightsail rights).
This is the only place AWS appears — the rest of the stack is provider-agnostic.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore

try:
    from .interface import HubHost, HubProvider, ssh_identity_args
except ImportError:  # pragma: no cover
    from interface import HubHost, HubProvider, ssh_identity_args  # type: ignore

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _endpoint_host(gateway_endpoint: Optional[str]) -> Optional[str]:
    if not gateway_endpoint:
        return None
    host = gateway_endpoint.strip().split(":", 1)[0].strip()
    return host or None


def _looks_like_instance_name(value: Optional[str]) -> bool:
    if not value:
        return False
    v = value.strip()
    if not v or _IPV4.match(v):
        return False
    return True


class LightsailProvider(HubProvider):
    name = "lightsail"

    def __init__(self, *, blueprint: str = "ubuntu_22_04",
                 bundle: str = "nano_3_0", region: str = "us-east-1") -> None:
        self._blueprint = blueprint
        self._bundle = bundle
        self._region = region

    def _client(self):
        if boto3 is None:
            raise RuntimeError("boto3 not installed; pip install boto3")
        return boto3.client("lightsail", region_name=self._region)

    def _list_instances(self, ls) -> list[dict[str, Any]]:
        return list(ls.get_instances().get("instances", []) or [])

    def _resolve_instance_name(
        self,
        ls,
        customer_id: str,
        *,
        instance_name: Optional[str] = None,
        hub_host_id: Optional[str] = None,
        gateway_endpoint: Optional[str] = None,
    ) -> tuple[str, bool]:
        """Return (instance_name, create_if_missing).

        ``create_if_missing`` is False when we adopted an existing hub.
        """
        canonical = f"kallon-hub-{customer_id}"
        instances = self._list_instances(ls)
        by_name = {i["name"]: i for i in instances if i.get("name")}

        # 1. Explicit override
        if _looks_like_instance_name(instance_name):
            name = instance_name.strip()  # type: ignore[union-attr]
            if name not in by_name:
                raise RuntimeError(
                    f"Lightsail instance {name!r} not found in {self._region}. "
                    f"Refusing to create under a non-canonical name."
                )
            return name, False

        # 2. Registry hub_host_id when it is an instance name (not a bare IP)
        if _looks_like_instance_name(hub_host_id):
            name = hub_host_id.strip()  # type: ignore[union-attr]
            if name not in by_name:
                raise RuntimeError(
                    f"Registry hub_host_id={name!r} is not a Lightsail instance in "
                    f"{self._region}. Pass --instance-name <existing> or fix the registry."
                )
            return name, False

        # 3. Match registry gateway public IP
        want_ip = _endpoint_host(gateway_endpoint)
        if want_ip:
            for inst in instances:
                if inst.get("publicIpAddress") == want_ip:
                    return inst["name"], False
            raise RuntimeError(
                f"Registry gateway_endpoint IP {want_ip} has no Lightsail instance in "
                f"{self._region}. Refusing to create a second hub (would orphan the "
                f"live gateway). Pass --instance-name <name> if the instance was renamed, "
                f"or delete/fix the endpoint first."
            )

        # 4. Tag kallon-customer=<customer_id>
        for inst in instances:
            tags = {t.get("key"): t.get("value") for t in (inst.get("tags") or [])}
            if tags.get("kallon-customer") == customer_id:
                return inst["name"], False

        # 5/6. Canonical name — reuse or create
        if canonical in by_name:
            return canonical, False
        return canonical, True

    def provision(
        self,
        customer_id: str,
        *,
        region: str | None = None,
        ssh_user: str = "ubuntu",
        dry_run: bool = False,
        instance_name: str | None = None,
        hub_host_id: str | None = None,
        gateway_endpoint: str | None = None,
        host: str | None = None,  # unused for lightsail; accepted for CLI symmetry
        **_,
    ) -> HubHost:
        # Allow --host to mean Lightsail instance name when it is not an IP.
        if instance_name is None and _looks_like_instance_name(host):
            instance_name = host

        if dry_run:
            resolved = instance_name or (
                hub_host_id if _looks_like_instance_name(hub_host_id) else None
            ) or f"kallon-hub-{customer_id}"
            return HubHost(host_id=resolved, public_ip="DRY_RUN_IP",
                           ssh_user=ssh_user, provider=self.name)

        if region:
            self._region = region
        ls = self._client()

        name, create = self._resolve_instance_name(
            ls,
            customer_id,
            instance_name=instance_name,
            hub_host_id=hub_host_id,
            gateway_endpoint=gateway_endpoint,
        )

        if create:
            ls.create_instances(
                instanceNames=[name],
                availabilityZone=f"{self._region}a",
                blueprintId=self._blueprint,
                bundleId=self._bundle,
                tags=[{"key": "kallon-customer", "value": customer_id}],
            )

        # Wait for running + public IP.
        public_ip = ""
        for _ in range(60):
            info = ls.get_instance(instanceName=name)["instance"]
            if info.get("state", {}).get("name") == "running" and info.get("publicIpAddress"):
                public_ip = info["publicIpAddress"]
                break
            time.sleep(5)
        if not public_ip:
            raise RuntimeError(f"Lightsail instance {name} not running in time")

        # Open SSH + WireGuard + Artemis hub tower-proxy + HLS.
        # put_instance_public_ports REPLACES the full public port set for the
        # instance — always include all required ports in one call.
        hub_proxy_port = int(os.environ.get("KALLON_HUB_PROXY_PORT", "8767") or "8767")
        hub_hls_port = int(os.environ.get("KALLON_HUB_HLS_PORT", "8768") or "8768")
        ls.put_instance_public_ports(
            instanceName=name,
            portInfos=[
                {"fromPort": 22, "toPort": 22, "protocol": "tcp"},
                {"fromPort": 51820, "toPort": 51820, "protocol": "udp"},
                {"fromPort": hub_proxy_port, "toPort": hub_proxy_port, "protocol": "tcp"},
                {"fromPort": hub_hls_port, "toPort": hub_hls_port, "protocol": "tcp"},
            ],
        )

        # Wait for SSH to accept connections.
        self._wait_ssh(public_ip, ssh_user)
        return HubHost(host_id=name, public_ip=public_ip,
                       ssh_user=ssh_user, provider=self.name)

    @staticmethod
    def _wait_ssh(host: str, user: str) -> None:
        import subprocess
        for _ in range(30):
            r = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=5", *ssh_identity_args(),
                 f"{user}@{host}", "true"],
                capture_output=True,
            )
            if r.returncode == 0:
                return
            time.sleep(5)
        raise RuntimeError(f"SSH to {user}@{host} never came up")

    def teardown(self, host: HubHost) -> None:
        self._client().delete_instance(instanceName=host.host_id)
