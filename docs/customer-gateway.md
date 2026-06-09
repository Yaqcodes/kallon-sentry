# Customer Hub Runbook (Terra-internal)

**Terra Industries · Internal Engineering**

How Terra ops stands up and manages a **customer hub** — one WireGuard hub VM
per customer org. The buyer never sees this; they only use the dashboard.

> Tooling: `infra/hub-provisioner/cli.py` (→ `kallon-hub-provision`),
> `scripts/kallon-gateway-init.sh`, `scripts/kallon-gateway-add-peer.sh`.
> Registry: `registry/cli.py` (→ `kallon-registry`). See also
> `kallon_mass_deployment_roadmap.md` §8 and `Considering physical server for VPS.md`.

---

## 1. Hosting options

| Option | Provider flag | When | Mechanism |
|--------|---------------|------|-----------|
| **B — API VPS** (default) | `--provider lightsail` | Retail customers | boto3 creates an Ubuntu Lightsail VM, opens UDP 51820 |
| **C — Manual / on-prem** | `--provider manual --host <ip>` | Enterprise, sovereign tier, customer DC | SSH to an existing Ubuntu host |
| A — Local VM (lab) | `--provider manual --host <lan-ip>` | Lab / fallback | same as C against a local VM |

All options converge on the **same** `kallon-gateway-init.sh` bring-up and the
same registry row + `gateway_manifest.json`. Swapping vendor = a new
`HubProvider` adapter only; core contracts (WireGuard, RTSP, HMAC alerts) never
change.

---

## 2. One-command provision

`kallon-hub-provision` does it all: ensures the customer row, provisions/verifies
the host, runs gateway bring-up, updates the registry to `active`, and writes the
manifest.

```bash
# Option B (default) — create an AWS Lightsail hub for a new customer
python infra/hub-provisioner/cli.py cust_acme \
    --provider lightsail --region us-east-2 \
    --subnet 10.51.0.0/24 --display-name "Acme Security"

# Option C / lab — use an existing Ubuntu host (e.g. current Lightsail box)
python infra/hub-provisioner/cli.py cust_lab \
    --provider manual --host 203.0.113.42 --ssh-user ubuntu \
    --subnet 10.50.0.0/24

# Plan only (no network, no DB writes beyond customer row): add --dry-run
```

Prereqs:

- Registry reachable (`KALLON_REGISTRY=postgres` + `DATABASE_URL`, or
  `--registry sqlite` for the lab).
- For Option B: AWS credentials with Lightsail rights + `boto3` installed.
- For all: `ssh`/`scp` on PATH and key-based access to the hub host.

---

## 3. What bring-up configures (on the hub)

`kallon-gateway-init.sh` (idempotent) performs:

1. Install `wireguard-tools`, `ufw`, `python3`.
2. Generate the gateway keypair (`/etc/wireguard/gateway.{private,public}`).
3. Write `/etc/wireguard/wg0.conf` (`[Interface]` only; `Address x.x.x.1/24`,
   `ListenPort 51820`).
4. `net.ipv4.ip_forward = 1`.
5. UFW: **51820/udp** open; **8080/tcp from the VPN subnet only**; SSH allowed;
   default deny incoming.
6. Install the HMAC alert listener as a systemd service
   (`kallon-alert-listener.service`, `infra/hub/alert_listener.py`).
7. Generate `/etc/kallon/alert.key` if absent — **copy this same value to every
   tower** for that customer (HMAC shared secret).
8. Emit `gateway_manifest.json` (gateway endpoint, public key, alert URL).

---

## 4. Adding towers as peers

**Production:** automatic on every enroll. The **enrollment API** SSHs to the
customer hub (`{gateway_host}` from the registry) using the **single Terra
hub-operations key** (`terra-hub-ops.pem` on the control plane). Same key is used
when `kallon-hub-provision` runs `gateway-init` on a new hub.

At hub bring-up, `kallon-gateway-init.sh --ops-ssh-pubkey-file …` installs
`terra-hub-ops.pub` into `ubuntu@hub` (set `KALLON_OPS_SSH_PUBKEY_FILE` on the
control plane). **Not one SSH key per customer VPS.**

See `docs/postgres-windows-server-setup.md` §7. **Do not** use `noop` in production.

**Manual peer add** — disaster recovery only, or towers provisioned before the
enrollment API existed:

```bash
scripts/kallon-gateway-add-peer.sh \
    --gateway-host 203.0.113.42 \
    --pubkey <jetson_public_key> \
    --vpn-ip 10.50.0.2/32 \
    --device-id kln_acme_000042
```

Idempotent: re-running replaces the peer's allowed-ips live (`wg set`) and
rewrites its block in `wg0.conf`. Never hand-edit `wg0.conf`.

---

## 5. `gateway_manifest.json`

Terra-internal artifact (written to `manifests/`); **never** sent to the buyer:

```json
{
  "customer_id": "cust_acme",
  "gateway_endpoint": "203.0.113.42:51820",
  "gateway_public_key": "<base64>",
  "vpn_subnet": "10.51.0.0/24",
  "gateway_ip": "10.51.0.1",
  "alert_webhook_url": "http://10.51.0.1:8080/alerts"
}
```

Consumed by the enrollment API (it hands the relevant fields to each tower at
first boot) and stored in the registry (`customers` row).

---

## 6. Verify a hub

```bash
# WireGuard up + peers
ssh ubuntu@<hub> 'sudo wg show wg0'
# Firewall posture
ssh ubuntu@<hub> 'sudo ufw status verbose'
# Alert listener health (from a VPN peer)
curl http://10.51.0.1:8080/healthz
# Registry reflects active hub
python -m registry.cli get-config --device kln_acme_000042
```

---

## 7. Decommission

```bash
# Lightsail: delete the instance (Option B)
#   handled by LightsailProvider.teardown(); or in the console as a last resort
# Registry: suspend the customer / towers
python -m registry.cli set-hub --customer cust_acme --status suspended
```

*The dashboard integration surface for a live hub is defined in
`docs/alert-webhook.md`.*
