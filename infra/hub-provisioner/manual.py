"""Manual / on-prem HubProvider (Option C).

No VM creation. The operator supplies an existing reachable Ubuntu host (a
customer DC box, a hand-made VPS, or the lab Lightsail instance). We only verify
SSH connectivity; bring-up is the shared run_gateway_init().
"""
from __future__ import annotations

import subprocess

try:  # works both as a package and as a flat script dir (hyphenated folder)
    from .interface import HubHost, HubProvider
except ImportError:  # pragma: no cover
    from interface import HubHost, HubProvider  # type: ignore


class ManualProvider(HubProvider):
    name = "manual"

    def provision(self, customer_id: str, *, host: str, ssh_user: str = "ubuntu",
                  dry_run: bool = False, **_) -> HubHost:
        if not host:
            raise ValueError("manual provider requires --host")
        hub = HubHost(host_id=host, public_ip=host, ssh_user=ssh_user, provider=self.name)
        if not dry_run:
            res = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=10", f"{ssh_user}@{host}", "true"],
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode != 0:
                raise RuntimeError(f"cannot SSH to {ssh_user}@{host}: {res.stderr.strip()}")
        return hub
