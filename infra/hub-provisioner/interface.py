"""HubProvider interface + shared remote bring-up helpers.

A HubProvider's only job is to make an Ubuntu host exist and reachable over SSH
with UDP 51820 open. The provider-agnostic `run_gateway_init()` then copies and
runs scripts/kallon-gateway-init.sh on that host and parses its manifest.

No Terraform, no AWS in the core — swapping VPS vendor is a new adapter only.
"""
from __future__ import annotations

import json
import os
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
def resolve_ssh_identity() -> Optional[str]:
    """Path to terra-hub-ops private key, or None if unset."""
    ident = os.environ.get("KALLON_OPS_SSH_IDENTITY_FILE", "").strip()
    if not ident:
        pub = os.environ.get("KALLON_OPS_SSH_PUBKEY_FILE", "").strip()
        if pub.endswith(".pub"):
            for candidate in (pub[:-4] + ".pem", pub[:-4]):
                if Path(candidate).is_file():
                    ident = candidate
                    break
    return ident or None


def ssh_identity_args(*, require: bool = False) -> list[str]:
    """Explicit -i for hub SSH. Subprocesses may not read ~/.ssh/config (common on Windows)."""
    ident = resolve_ssh_identity()
    if ident:
        path = Path(ident)
        if not path.is_file():
            msg = (
                f"KALLON_OPS_SSH_IDENTITY_FILE={ident!r} not found. "
                "Run scripts/install-terra-hub-ops-key.ps1 on the control plane."
            )
            if require:
                raise RuntimeError(msg)
            return ["-o", "BatchMode=yes"]
        return [
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-i", str(path),
        ]
    if require:
        raise RuntimeError(
            "KALLON_OPS_SSH_IDENTITY_FILE is not set (private .pem required for hub SSH on Windows). "
            "Set it to C:\\kallon\\secrets\\terra-hub-ops.pem after install-terra-hub-ops-key.ps1."
        )
    return ["-o", "BatchMode=yes"]


def _ssh_base(host: HubHost) -> list[str]:
    return [
        "ssh", "-o", "StrictHostKeyChecking=accept-new",
        *ssh_identity_args(),
        f"{host.ssh_user}@{host.public_ip}",
    ]


def _scp(host: HubHost, src: Path, dst: str) -> None:
    subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=accept-new",
         *ssh_identity_args(),
         str(src), f"{host.ssh_user}@{host.public_ip}:{dst}"],
        check=True, timeout=120,
    )


def _strip_crlf_on_hub(host: HubHost, *remote_paths: str) -> None:
    """Windows git checkouts often use CRLF; bash on Ubuntu rejects 'pipefail\\r'."""
    if not remote_paths:
        return
    quoted = " ".join(shlex.quote(p) for p in remote_paths)
    subprocess.run(
        [*_ssh_base(host), f"for f in {quoted}; do sed -i 's/\\r$//' \"$f\"; done"],
        check=True, timeout=60,
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
    ops_pubkey_file = os.environ.get("KALLON_OPS_SSH_PUBKEY_FILE", "").strip()
    ops_user = os.environ.get("KALLON_OPS_SSH_USER", host.ssh_user)
    remote_cmd = (
        f"sudo bash /tmp/kallon-gateway-init.sh "
        f"--customer-id {shlex.quote(customer_id)} "
        f"--vpn-subnet {shlex.quote(vpn_subnet)} "
        f"--public-endpoint {shlex.quote(public_endpoint)} "
        f"--ops-ssh-user {shlex.quote(ops_user)}"
    )
    if ops_pubkey_file and Path(ops_pubkey_file).is_file():
        remote_cmd += f" --ops-ssh-pubkey-file /tmp/kallon-ops.pub"
    if dry_run:
        scp_files = [str(GATEWAY_INIT), str(ALERT_LISTENER)]
        if ops_pubkey_file and Path(ops_pubkey_file).is_file():
            scp_files.append(ops_pubkey_file)
        return {
            "_dry_run": True,
            "customer_id": customer_id,
            "vpn_subnet": vpn_subnet,
            "would_scp": scp_files,
            "would_run": remote_cmd,
            "gateway_endpoint": f"{public_endpoint}:51820",
        }

    # Stage the init script and listener, mirroring the repo layout the script
    # expects (../infra/hub/alert_listener.py relative to the script).
    _ssh = _ssh_base(host)
    subprocess.run([*_ssh, "mkdir -p /tmp/infra/hub"], check=True, timeout=60)
    _scp(host, GATEWAY_INIT, "/tmp/kallon-gateway-init.sh")
    if ops_pubkey_file and Path(ops_pubkey_file).is_file():
        _scp(host, Path(ops_pubkey_file), "/tmp/kallon-ops.pub")
    staged: list[str] = ["/tmp/kallon-gateway-init.sh"]
    if ops_pubkey_file and Path(ops_pubkey_file).is_file():
        staged.append("/tmp/kallon-ops.pub")
    if ALERT_LISTENER.exists():
        _scp(host, ALERT_LISTENER, "/tmp/infra/hub/alert_listener.py")
        staged.append("/tmp/infra/hub/alert_listener.py")
        # The script resolves the listener via $(dirname $0)/../infra/hub/...
        subprocess.run([*_ssh, "cp /tmp/kallon-gateway-init.sh /tmp/scripts_init.sh 2>/dev/null || true"],
                       check=False, timeout=30)
    _strip_crlf_on_hub(host, *staged)
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
