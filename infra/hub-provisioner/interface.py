"""HubProvider interface + shared remote bring-up helpers.

A HubProvider's only job is to make an Ubuntu host exist and reachable over SSH
with UDP 51820 open. The provider-agnostic `run_gateway_init()` then copies and
runs scripts/kallon-gateway-init.sh on that host and parses its manifest.

No Terraform, no AWS in the core — swapping VPS vendor is a new adapter only.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_INIT = REPO_ROOT / "scripts" / "kallon-gateway-init.sh"
ALERT_LISTENER = REPO_ROOT / "infra" / "hub" / "alert_listener.py"


@dataclass
class HubHost:
    """A reachable Ubuntu host that will become the customer hub."""
    host_id: str            # provider instance id, or the host string for manual
    public_ip: str
    ssh_user: str = "ubuntu"
    provider: str = "manual"


class HubProvider(ABC):
    name: str = "manual"

    @abstractmethod
    def provision(self, customer_id: str, **kwargs) -> HubHost:
        """Create/verify the hub host and return connection details."""

    def teardown(self, host: HubHost) -> None:  # optional
        raise NotImplementedError(f"{self.name} provider does not support teardown")


# ── remote bring-up (provider-agnostic) ──────────────────────────────────────
def _ssh_base(host: HubHost) -> list[str]:
    return [
        "ssh", "-o", "StrictHostKeyChecking=accept-new",
        f"{host.ssh_user}@{host.public_ip}",
    ]


def _scp(host: HubHost, src: Path, dst: str) -> None:
    subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=accept-new",
         str(src), f"{host.ssh_user}@{host.public_ip}:{dst}"],
        check=True, timeout=120,
    )


def run_gateway_init(
    host: HubHost,
    *,
    customer_id: str,
    vpn_subnet: str,
    public_endpoint: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Copy + run kallon-gateway-init.sh on the hub; return the manifest dict."""
    public_endpoint = public_endpoint or host.public_ip
    remote_cmd = (
        f"sudo bash /tmp/kallon-gateway-init.sh "
        f"--customer-id {shlex.quote(customer_id)} "
        f"--vpn-subnet {shlex.quote(vpn_subnet)} "
        f"--public-endpoint {shlex.quote(public_endpoint)}"
    )
    if dry_run:
        return {
            "_dry_run": True,
            "customer_id": customer_id,
            "vpn_subnet": vpn_subnet,
            "would_scp": [str(GATEWAY_INIT), str(ALERT_LISTENER)],
            "would_run": remote_cmd,
            "gateway_endpoint": f"{public_endpoint}:51820",
        }

    # Stage the init script and listener, mirroring the repo layout the script
    # expects (../infra/hub/alert_listener.py relative to the script).
    _ssh = _ssh_base(host)
    subprocess.run([*_ssh, "mkdir -p /tmp/infra/hub"], check=True, timeout=60)
    _scp(host, GATEWAY_INIT, "/tmp/kallon-gateway-init.sh")
    if ALERT_LISTENER.exists():
        _scp(host, ALERT_LISTENER, "/tmp/infra/hub/alert_listener.py")
        # The script resolves the listener via $(dirname $0)/../infra/hub/...
        subprocess.run([*_ssh, "cp /tmp/kallon-gateway-init.sh /tmp/scripts_init.sh 2>/dev/null || true"],
                       check=False, timeout=30)
    proc = subprocess.run([*_ssh, remote_cmd], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"gateway-init failed: {proc.stderr.strip()}")

    # The manifest is the JSON object printed to stdout.
    text = proc.stdout
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"no manifest JSON in gateway-init output:\n{text}")
    return json.loads(text[start:end + 1])
