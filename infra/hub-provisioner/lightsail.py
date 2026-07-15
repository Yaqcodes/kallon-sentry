"""AWS Lightsail HubProvider (default Option B adapter).

Creates a small Ubuntu Lightsail instance, opens UDP 51820 (WireGuard) and
TCP 8767 (hub tower-proxy for Artemis), waits for SSH, and returns its
static/public IP. Bring-up is the shared run_gateway_init().

Requires boto3 + AWS credentials (e.g. an ops IAM user with Lightsail rights).
This is the only place AWS appears — the rest of the stack is provider-agnostic.
"""
from __future__ import annotations

import os
import time

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore

try:
    from .interface import HubHost, HubProvider, ssh_identity_args
except ImportError:  # pragma: no cover
    from interface import HubHost, HubProvider, ssh_identity_args  # type: ignore


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

    def provision(self, customer_id: str, *, region: str | None = None,
                  ssh_user: str = "ubuntu", dry_run: bool = False, **_) -> HubHost:
        instance_name = f"kallon-hub-{customer_id}"
        if dry_run:
            return HubHost(host_id=instance_name, public_ip="DRY_RUN_IP",
                           ssh_user=ssh_user, provider=self.name)

        if region:
            self._region = region
        ls = self._client()

        existing = {i["name"] for i in ls.get_instances().get("instances", [])}
        if instance_name not in existing:
            ls.create_instances(
                instanceNames=[instance_name],
                availabilityZone=f"{self._region}a",
                blueprintId=self._blueprint,
                bundleId=self._bundle,
                tags=[{"key": "kallon-customer", "value": customer_id}],
            )

        # Wait for running + public IP.
        public_ip = ""
        for _ in range(60):
            info = ls.get_instance(instanceName=instance_name)["instance"]
            if info.get("state", {}).get("name") == "running" and info.get("publicIpAddress"):
                public_ip = info["publicIpAddress"]
                break
            time.sleep(5)
        if not public_ip:
            raise RuntimeError(f"Lightsail instance {instance_name} not running in time")

        # Open SSH + WireGuard + Artemis hub tower-proxy.
        # put_instance_public_ports REPLACES the full public port set for the
        # instance — always include all required ports in one call.
        hub_proxy_port = int(os.environ.get("KALLON_HUB_PROXY_PORT", "8767") or "8767")
        ls.put_instance_public_ports(
            instanceName=instance_name,
            portInfos=[
                {"fromPort": 22, "toPort": 22, "protocol": "tcp"},
                {"fromPort": 51820, "toPort": 51820, "protocol": "udp"},
                {"fromPort": hub_proxy_port, "toPort": hub_proxy_port, "protocol": "tcp"},
            ],
        )

        # Wait for SSH to accept connections.
        self._wait_ssh(public_ip, ssh_user)
        return HubHost(host_id=instance_name, public_ip=public_ip,
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
