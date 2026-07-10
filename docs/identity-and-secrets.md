# Kallon Identity & Secrets Standard

**Terra Industries · Internal Engineering**

Canonical reference for every identifier and secret in the Kallon fleet. The
machine-enforced source of truth is `registry/identity.py`; this document is the
human-readable companion. Keep them in sync.

> Rule of thumb: **identifiers are public and log-safe; secrets never appear in
> the registry, logs, or git.** Only public keys and metadata are stored
> centrally.

---

## 1. Identifiers

| Field | Format | Regex | Example |
|-------|--------|-------|---------|
| `customer_id` | `cust_<slug>` | `^cust_[a-z0-9]+$` | `cust_acme` |
| `device_id` | `kln_<slug>_<6-digit serial>` | `^kln_[a-z0-9]+_\d{6}$` | `kln_acme_000042` |
| `gateway_id` | `gw_<slug>` | `^gw_[a-z0-9]+$` | `gw_acme` |
| `group_id` (optional) | `grp_<slug>_<site>` | `^grp_[a-z0-9]+_[a-z0-9]+$` | `grp_acme_north` |
| `claim_code` | `clm_<base64url 16B>` | `^clm_[A-Za-z0-9_-]{22}$` | `clm_8f3kLmNpQr...` |
| `enrollment_token` | `enr_<base64url 32B>` | `^enr_[A-Za-z0-9_-]{43}$` | `enr_x9...` |
| `confirm_token` | `cnf_<base64url 24B>` | issued by enrollment API | `cnf_...` |

- **slug** is `[a-z0-9]+` (lowercase). Derive once per customer; never change it.
- Construct IDs via `registry.identity` helpers (`customer_id()`, `device_id()`,
  `new_claim_code()`, `new_enrollment_token()`), never by string formatting in
  ad-hoc scripts.

### VPN IP allocation (per customer `/24`)

| Address | Role |
|---------|------|
| `x.x.x.1` | Hub (`wg0` gateway) |
| `x.x.x.10` | Reserved — NOC / ops laptop WG peer |
| `x.x.x.2 – .99` | Towers (registry allocator, monotonic) |
| `x.x.x.100 – .254` | Spare / future |

Customer `/24` subnets are assigned at order fulfillment (`registry/subnet.py`:
`10.50.0.0/24`, `10.51.0.0/24`, …). Tower host IPs inside each subnet are
allocated at enroll (`ip_allocations.next_host_octet`, row-locked in Postgres).

---

## 2. Secrets

| Secret | Generation | Lives on | Mode | In registry? |
|--------|-----------|----------|------|--------------|
| WireGuard **private** key (tower) | `wg genkey` on-device | `/etc/wireguard/jetson.private` | `600` | **No** |
| WireGuard **public** key (tower) | derived | device + hub peer + registry | `644` | Yes (public) |
| WireGuard private key (hub) | `wg genkey` on hub | `/etc/wireguard/gateway.private` | `600` | **No** |
| WireGuard public key (hub) | derived | registry (`gateway_public_key`) | `644` | Yes (public) |
| Alert **HMAC** key | `openssl rand -base64 32` | `/etc/kallon/alert.key` on **tower + hub** | `640` | **No** |
| Camera password | operator-set | `/etc/kallon/device.env`, `/etc/mediamtx.yml` | `640` | **No** |
| `enrollment_token` | `new_enrollment_token()` at factory | `device.env` (one-time); **hash** in registry | `640` | hash only |
| `confirm_token` | enrollment API per enroll | transient (in-memory on API) | — | **No** |
| `ENROLLMENT_HMAC_KEY` (service) | `openssl rand -base64 32` | API host `enrollment-api.env`; baked into factory image | `600` | **No** |
| `DATABASE_URL` | — | API host `enrollment-api.env` | `600` | **No** |
| **Terra hub-ops SSH** private key | `ssh-keygen` once | Control plane `C:\kallon\secrets\terra-hub-ops.pem` | `600` | **No** |
| Terra hub-ops SSH public key | derived | Installed on **every** hub at `gateway-init` | `644` | **No** |

Hub VPN **peer forwarding** (`ufw route allow in on wg0 out on wg0`) is applied by
`kallon-gateway-init.sh` on new hubs. Existing hubs: `kallon-gateway-ensure-forwarding.sh`
(**hub VPS only** — not towers). Required for NOC/dashboard RTSP to towers. See
`docs/postgres-windows-server-setup.md` §8.1.

### Enrollment API environment (production — Path P)

On the Windows Server, `C:\kallon\config\enrollment-api.env` (or Linux
`/etc/kallon/enrollment-api.env`) must include:

| Variable | Production value |
|----------|------------------|
| `KALLON_REGISTRY` | `postgres` |
| `DATABASE_URL` | `postgresql://kallon:…@127.0.0.1:5432/kallon` |
| `KALLON_PEER_BACKEND` | **`subprocess`** (never `noop` in prod) |
| `KALLON_ADDPEER_CMD` | Template invoking `kallon-gateway-add-peer.sh` — see `docs/postgres-windows-server-setup.md` §7 |
| `KALLON_OPS_SSH_PUBKEY_FILE` | Path to `terra-hub-ops.pub` — hub provisioner installs on each hub |
| `KALLON_OPS_SSH_IDENTITY_FILE` | Path to `terra-hub-ops.pem` — **required** for Python `ssh`/`scp` on Windows |

**One ops keypair** serves hub provisioner + enrollment peer-add for **all** customer
hubs. Not one PEM per VPS. Postgres binds `localhost` only; the API binds
`127.0.0.1:8000` with TLS on `:443`. Towers use `ENROLLMENT_URL=https://enroll.<domain>/v1`.

> `KALLON_PEER_BACKEND` **defaults to `subprocess`** if unset — the API refuses
> to silently no-op peer-add in an unconfigured deployment. `noop` only
> activates if you set it explicitly (tests / deliberate Path B lab runs), and
> every use logs at `ERROR` so it can't be mistaken for normal production
> behavior. Still set it explicitly in `enrollment-api.env` for clarity — see
> `docs/postgres-windows-server-setup.md` §7.4 for where the logs land and how
> to read them.

### Key facts

- The **enrollment token is one-time**: the registry stores only its SHA-256
  hash (`towers.enrollment_token_hash`). The plaintext is printed **once** by
  `register-tower` for factory bake-in.
- The **alert HMAC key** must be identical on the tower and its customer hub. It
  is the shared secret for `X-Kallon-Signature` (see `docs/alert-webhook.md`).
- `AllowedIPs` on the tower is scoped to the customer subnet — **never**
  `0.0.0.0/0`.

---

## 3. Tower config on the Jetson

All per-tower secrets and network identity live under `/etc/kallon/`. The installer
and every systemd unit read **`/etc/kallon/device.env`** — not a copy in the repo
or home directory. Create this directory and install the files **before** the first
`sudo scripts/kallon-jetson-install.sh` run.

### 3.1 Paths and permissions

```
/etc/kallon/
  ├── device.env          0640 root:RUNTIME_USER   # config + camera password + token
  ├── alert.key           0640 root:RUNTIME_USER   # HMAC shared with hub
  └── .enrolled           0644 root:root            # marker; presence = enrolled
/etc/wireguard/
  ├── jetson.private      0600 root:root            # NEVER leaves the device
  ├── jetson.public       0644 root:root
  └── wg0.conf            0600 root:root            # rendered, not hand-edited
```

`RUNTIME_USER` is the Jetson login that runs Kallon services. Legacy bench
images used `khalifa`; a fresh flash uses whatever account you created during
setup — **do not assume `khalifa` exists**.

> **Recommendation — set `RUNTIME_USER` explicitly in `device.env`.** For a
> true golden image, set `RUNTIME_USER=<your-login>` (e.g. `RUNTIME_USER=sentinel`)
> in `device.env`. It removes all ambiguity and documents intent, guaranteeing
> the same result on every device and every invocation method. If left unset,
> the installer falls back to `SUDO_USER`, then `logname`; if **both** are empty
> (e.g. run from a plain root shell or a first-boot script), the installer now
> **fails loudly** rather than silently guessing a user.

`.gitignore` already blocks `device.env`, `alert.key`, `*.private`, `*.pem`,
`*.key`, `*token*`, and `wg-keys*`. Only `*.example` templates are committed.

### 3.2 Installing `device.env` and `alert.key`

Install both files on the Jetson **before** `kallon-jetson-install.sh`. They are
independent: `device.env` is **per tower** (from fulfill-order); `alert.key` is
**per hub** (same bytes on every tower for that customer — fetch from the hub,
not generated per tower).

#### A. Copy to the Jetson (Windows control plane)

From the operator PC after `kallon-fulfill-order` (or when restoring a backed-up
tower):

```powershell
$HUB_HOST = "YOUR_HUB_PUBLIC_IP"              # e.g. from fulfillment manifest
$JETSON = "YOUR_USER@JETSON_LAN_IP"            # SSH login on the tower
$PEM = "C:\kallon\secrets\terra-hub-ops.pem"
$FACTORY = "C:\kallon\factory\cust_<slug>"     # fulfill-order output directory

# Per tower — fulfillment renders device_kln_<slug>_00000N.env
scp "$FACTORY\device_kln_<slug>_000001.env" "${JETSON}:/tmp/device.env"

# Per hub — one alert.key shared by all towers on that hub (fetch once, reuse)
ssh -i $PEM "ubuntu@${HUB_HOST}" "sudo cat /etc/kallon/alert.key" `
  | Set-Content -NoNewline -Encoding ascii "$FACTORY\alert.key"
scp "$FACTORY\alert.key" "${JETSON}:/tmp/alert.key"
```

Keep local backups alongside factory output (e.g. `device.env.backup`,
`alert.key.backup`) when re-flashing a lab tower.

#### B. Install on the Jetson

SSH to the tower. Replace `SOURCE.env` with `/tmp/device.env` (from step A),
a backed-up `device.env`, or `deploy/device.env.example` (bench).

```bash
# Your Jetson login — NOT necessarily "khalifa" on a fresh image
RUNTIME_USER="${SUDO_USER:-$(logname 2>/dev/null || id -un)}"
SOURCE=/tmp/device.env

# 1. Create config directory (required — install fails without this)
sudo install -d -m 0750 -o root -g "$RUNTIME_USER" /etc/kallon

# 2. Install device.env with correct owner and mode
sudo install -m 0640 -o root -g "$RUNTIME_USER" "$SOURCE" /etc/kallon/device.env

# 3. Strip Windows CRLF (fixes: $'\r': command not found when sourcing)
sudo sed -i 's/\r$//' /etc/kallon/device.env

# 4. Edit secrets and iface names if needed (CAMERA_PASSWORD, WAN_IFACE, …)
sudoedit /etc/kallon/device.env

# 5. Install alert.key — must match the hub exactly
sudo install -m 0640 -o root -g "$RUNTIME_USER" /tmp/alert.key /etc/kallon/alert.key
sudo sed -i 's/\r$//' /etc/kallon/alert.key
```

**Verify:**

```bash
ls -la /etc/kallon/
grep DEVICE_ID /etc/kallon/device.env
```

**Then run the installer:**

```bash
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env
```

---

## 4. Rotation procedures

### 4.1 Rotate a tower WireGuard keypair

1. On the tower: `sudo scripts/kallon-wg-provision.sh --regenerate-keys` →
   prints the new public key.
2. Update the hub peer + registry: `scripts/kallon-gateway-add-peer.sh` with the
   new pubkey/IP (idempotent), and `kallon-registry set-hub` / re-enroll as
   needed. Remove the stale peer from `wg0.conf` on the hub.
3. `sudo systemctl restart wg-quick@wg0`; confirm a fresh handshake.

### 4.2 Rotate the alert HMAC key

1. Generate: `openssl rand -base64 32`.
2. Write the **same** value to `/etc/kallon/alert.key` on the tower **and** the
   hub (mode `640`).
3. `sudo systemctl restart kallon-watchdog` (tower) and the alert listener (hub).
   Rotate during a maintenance window — in-flight alerts signed with the old key
   will fail verification.

### 4.3 Rotate the enrollment token (pre-enrollment only)

- Tokens are consumed at enroll. To re-issue before shipping, re-run
  `kallon-registry register-tower` semantics (new token hash) or add a dedicated
  `rotate-token` command. Never reuse a token across devices.

### 4.4 Rotate the service `ENROLLMENT_HMAC_KEY`

- Update `enrollment-api.env` on the API host **and** rebuild the factory image
  with the new value, then restart `kallon-enrollment-api`. Stagger so in-field
  first-boots are not stranded mid-rollout.

---

## 5. Audit

Every mutating registry action writes an `audit_events` row (`customer_created`,
`tower_registered`, `ip_allocated`, `tower_enrolled`, `tower_active`,
`enroll_rejected`, …) with `entity_id` and `actor`. The enrollment API uses
actor `enrollment-api`; the CLI uses `cli` (override with `--actor`).

---

*Keep in sync with `registry/identity.py` and `planning/mass-deployment-roadmap.md` §3.*
