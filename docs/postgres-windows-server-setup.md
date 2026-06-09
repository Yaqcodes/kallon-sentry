# Kallon Control Plane on Windows Server

**Terra Industries · Internal Engineering**

**Production path (Path P):** stand up the Terra control plane on Windows Server —
**PostgreSQL 16** registry, **enrollment API** (HTTPS, automated hub peer-add), and
registry/hub-provisioner CLI. Towers enroll over the public internet; Postgres stays
on `localhost` only.

| Related doc | Role |
|-------------|------|
| **`docs/field-test-setup.md`** | **End-to-end flow** — Path A → Path P → §5 Jetson |
| `kallon_mass_deployment_roadmap.md` | Registry design §5, Phase 2–3 deliverables |
| `Considering physical server for VPS.md` | Control plane layout, exposure rules |
| `docs/identity-and-secrets.md` | `DATABASE_URL`, enrollment tokens, HMAC keys |
| `docs/customer-gateway.md` | Hub provision + peer-add contract |

> **Security:** Postgres must **not** be exposed to the public internet. Bind to
> `localhost` or a private LAN/ops VPN only. Enrollment API is the factory/tower-facing
> interface; towers never connect to Postgres directly.

---

## What you are building

Postgres holds the fleet registry:

- `customers` — one row per customer org (`cust_*`)
- `towers` — one row per manufactured unit (`kln_*`)
- `ip_allocations` — monotonic VPN host octet allocator per customer
- `audit_events` — ops audit trail

Applications connect via:

```text
KALLON_REGISTRY=postgres
DATABASE_URL=postgresql://kallon:<password>@127.0.0.1:5432/kallon
```

Schema is applied by the repo — not hand-written SQL in production:

```powershell
python -m registry.cli init-schema
```

---

## 1. Install PostgreSQL 16

1. Download the **PostgreSQL 16** Windows installer from
   [postgresql.org/download/windows](https://www.postgresql.org/download/windows/)
   (EDB installer).
2. Run the installer:
   - **Components:** PostgreSQL Server, Command Line Tools (pgAdmin optional)
   - **Port:** `5432` (default)
   - **Superuser (`postgres`) password:** strong password; store in password manager
   - **Locale:** default is fine
3. Finish — service name is typically `postgresql-x64-16`.

Verify:

```powershell
psql -U postgres -h localhost -c "SELECT version();"
```

If `psql` is not on PATH, use the full path:

```text
C:\Program Files\PostgreSQL\16\bin\psql.exe
```

---

## 2. Create database and application user

Open psql as superuser:

```powershell
psql -U postgres -h localhost
```

```sql
CREATE USER kallon WITH PASSWORD 'choose-a-strong-password-here';
CREATE DATABASE kallon OWNER kallon;
GRANT ALL PRIVILEGES ON DATABASE kallon TO kallon;
\q
```

Test the application user:

```powershell
psql -U kallon -h localhost -d kallon -c "SELECT 1;"
```

| Item | Value |
|------|--------|
| Database | `kallon` |
| App user | `kallon` |
| Port | `5432` |
| Connection string | `postgresql://kallon:<password>@127.0.0.1:5432/kallon` |

---

## 3. Network hardening

On the control plane, Postgres should accept connections **only from localhost**
(same machine as enrollment API) or from a **private LAN** if the API runs elsewhere.

### 3.1 `postgresql.conf`

Path (default): `C:\Program Files\PostgreSQL\16\data\postgresql.conf`

**Enrollment API on the same server** (recommended for solo / lab):

```ini
listen_addresses = 'localhost'
```

**Enrollment API on another host on your LAN:**

```ini
listen_addresses = '127.0.0.1,<server-private-ip>'
```

### 3.2 `pg_hba.conf`

Same `data` directory:

```text
# TYPE  DATABASE  USER    ADDRESS           METHOD
host    kallon    kallon  127.0.0.1/32      scram-sha-256
host    kallon    kallon  192.168.1.0/24    scram-sha-256
```

Adjust the subnet to match your ops LAN. Do **not** add `0.0.0.0/0`.

Restart PostgreSQL:

```powershell
Restart-Service postgresql-x64-16
```

### 3.3 Windows Firewall

- Do **not** open inbound TCP 5432 to the internet.
- If LAN access is required, allow 5432 **only** from the enrollment API host IP.

---

## 4. Python dependencies

On the machine that runs registry CLI and/or enrollment API (often the same server):

```powershell
cd "C:\path\to\kallon-sentry\CODE"
pip install -r registry/requirements.txt
pip install -r infra/enrollment-api/requirements.txt
```

`registry/requirements.txt` installs `psycopg[binary]` (required by
`registry/postgres_provider.py`).

---

## 5. Initialize schema

Set environment variables for the session:

```powershell
$env:KALLON_REGISTRY = "postgres"
$env:DATABASE_URL = "postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon"

cd "C:\path\to\kallon-sentry\CODE"
python -m registry.cli init-schema
```

| Expected | `{"ok": true, "action": "init-schema"}` |
| If it fails | Check password, service running, `psycopg` installed |

Verify tables:

```powershell
psql -U kallon -h localhost -d kallon -c "\dt"
```

Expected tables: `customers`, `towers`, `ip_allocations`, `audit_events`.

---

## 6. Optional — registry CLI smoke test

**Skip this step** if you are proceeding to §8 (`kallon-hub-provision`). Hub
provisioner **creates the customer row for you** when you pass `--subnet` — that is
the production path.

This section exists only to verify Postgres + Python can write a row **before**
you configure SSH, the enrollment API, or a hub:

```powershell
$env:KALLON_REGISTRY = "postgres"
$env:DATABASE_URL = "postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon"

python -m registry.cli create-customer --slug lab --name "Kallon Lab" --subnet 10.50.0.0/24 --provider manual
python -m registry.cli list-customers
```

| What it does | Inserts one `customers` row + `ip_allocations` row in Postgres |
| Why it exists | Isolated check after `init-schema` — proves the registry CLI talks to the DB |
| Production? | **No** — use §8 instead; do not create `cust_lab` here *and* again in §8 |

If you already ran §6 and then run §8 for the same `cust_lab` / subnet, hub
provisioner will find the existing customer and continue (no duplicate).

---

## 7. Terra hub operations SSH key (one key — not per customer hub)

The control plane SSHs to **every** customer hub for two jobs:

| Caller | When | Script |
|--------|------|--------|
| **Hub provisioner** | New `cust_*` hub bring-up | `kallon-gateway-init.sh` over SSH |
| **Enrollment API** | Each tower `POST /v1/enroll` | `kallon-gateway-add-peer.sh` over SSH |

You need **one Terra hub-operations keypair** on the Windows Server — **not** a new
PEM per VPS or per customer.

```text
Windows Server                          Customer hubs (N VMs)
┌─────────────────────────┐            ┌──────────────────────┐
│ terra-hub-ops.pem       │─── SSH ───►│ cust_lab  18.220…    │
│ terra-hub-ops.pub       │─── SSH ───►│ cust_acme 203.0…     │
│ (enrollment API +       │─── SSH ───►│ cust_beta …          │
│  hub-provisioner)       │            └──────────────────────┘
└─────────────────────────┘
```

### 7.1 Use your existing Lightsail PEM (recommended)

Your `kallon-vps-key.pem` (the key you already use for `ssh ubuntu@18.220.75.237`)
**becomes** the fleet ops key. Do **not** generate a new key unless you intend to
rotate — a new key would not work on the existing hub until you install its `.pub`.

On the **Windows Server** (as the user that will run the enrollment API):

```powershell
# One-shot install: copy PEM, fix ACLs, derive .pub, smoke-test SSH
cd C:\path\to\kallon-sentry\CODE

powershell -ExecutionPolicy Bypass -File .\scripts\install-terra-hub-ops-key.ps1 `
  -SourcePem "C:\path\to\kallon-vps-key.pem" `
  -HubHost 18.220.75.237

# Re-run ACL/.pub fix only (if terra-hub-ops.pem already exists):
# powershell -ExecutionPolicy Bypass -File .\scripts\install-terra-hub-ops-key.ps1 -Repair

# Set for this PowerShell session (hub-provisioner + enrollment API)
$env:KALLON_OPS_SSH_IDENTITY_FILE = "C:\kallon\secrets\terra-hub-ops.pem"
$env:KALLON_OPS_SSH_PUBKEY_FILE = "C:\kallon\secrets\terra-hub-ops.pub"

powershell -ExecutionPolicy Bypass -File .\scripts\kallon-hub-ssh-verify.ps1 -HubHost 18.220.75.237
```

> **`-SourcePem`:** your **original** Lightsail PEM (`kallon-vps-key.pem`), **not**
> `C:\kallon\secrets\terra-hub-ops.pem` (copy-to-self error). If already installed,
> use `-Repair` instead.

> **Verify output:** **Test 1** is what hub-provisioner uses — if it passes, proceed
> to §8. **Test 2** (plain `ssh`) is optional; failure means fix `~/.ssh/config`
> (`Host *` IdentityFile), not a blocker for production.

These `.ps1` helpers are **optional** conveniences for Path P (Windows control plane).
They are not required for production logic — hub-provisioner and the enrollment API
only need the files at `C:\kallon\secrets\` and env vars. Linux control plane: see
`docs/field-test-setup.md` §6 (`chmod 600`, same env var names).

Manual equivalent (if you cannot run the script):

```powershell
New-Item -ItemType Directory -Force -Path C:\kallon\secrets | Out-Null
Copy-Item "C:\Users\kayob\Documents\Khalifa Projects\Kallon Sentry Tower\kallon-vps-key.pem" `
  "C:\kallon\secrets\terra-hub-ops.pem"

# OpenSSH rejects keys readable by Administrators / SYSTEM — current user ONLY
icacls C:\kallon\secrets /inheritance:r
icacls C:\kallon\secrets /grant:r "$($env:USERNAME):(OI)(CI)F"
icacls C:\kallon\secrets\terra-hub-ops.pem /inheritance:r
icacls C:\kallon\secrets\terra-hub-ops.pem /grant:r "$($env:USERNAME):(R)"
icacls C:\kallon\secrets\terra-hub-ops.pem /remove "NT AUTHORITY\SYSTEM" "BUILTIN\Administrators" "Everyone"

cmd /c "ssh-keygen -y -f C:\kallon\secrets\terra-hub-ops.pem > C:\kallon\secrets\terra-hub-ops.pub"

$env:KALLON_OPS_SSH_IDENTITY_FILE = "C:\kallon\secrets\terra-hub-ops.pem"
$env:KALLON_OPS_SSH_PUBKEY_FILE = "C:\kallon\secrets\terra-hub-ops.pub"

ssh -i C:\kallon\secrets\terra-hub-ops.pem -o IdentitiesOnly=yes ubuntu@18.220.75.237 "sudo wg show wg0 public-key"
```

| File | Role |
|------|------|
| `terra-hub-ops.pem` | Private key — SSH from control plane to any hub |
| `terra-hub-ops.pub` | Public key — `gateway-init` installs on **new** hubs |

> **Python / hub-provisioner:** interactive `ssh` reads `~/.ssh/config`; Python
> subprocesses often do **not** on Windows. Always set **`KALLON_OPS_SSH_IDENTITY_FILE`**
> to the `.pem` path when running `kallon-hub-provision` or the enrollment API —
> not just the `.pub` file.

**First hub (`18.220.75.237`):** Lightsail already authorized this key when the
instance was created. SSH works immediately; you do **not** need to re-install the
pubkey on that box unless you rotate keys.

**Only if starting fresh** (no existing PEM):

```powershell
ssh-keygen -t ed25519 -f C:\kallon\secrets\terra-hub-ops -C "terra-hub-ops@control-plane" -N ""
```

Set this env var for **hub provisioner** runs and in the enrollment API service
environment (see §7.3). `kallon-gateway-init.sh` installs `terra-hub-ops.pub` into
each hub's `authorized_keys` at provision time (idempotent).

**Lightsail (Option B):** use the **same** key pair for all instances in a region
(either Terra's `terra-hub-ops` uploaded as a Lightsail key pair, or the account
default key — one download per region, not per customer). New VMs do not get new PEMs.

**First hub already live** (`18.220.75.237`): if it was built with `terra-hub-ops.pem`
already, no change. If you rotate keys later, re-run gateway-init with
`--ops-ssh-pubkey-file` or append the new `.pub` once.

### 7.2 SSH client config (optional)

**Do not use `Host *` with `IdentityFile`** — it breaks all SSH (GitHub, other servers)
if the ops PEM is missing or has bad ACLs. Use a **host alias** instead:

```text
# C:\Users\<service-account>\.ssh\config
Host kallon-hub-*
  User ubuntu
  IdentityFile C:\kallon\secrets\terra-hub-ops.pem
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

Connect with an alias (optional convenience):

```powershell
ssh kallon-hub-lab@18.220.75.237 "sudo wg show wg0 public-key"
```

For hub-provisioner and the enrollment API, **`KALLON_OPS_SSH_IDENTITY_FILE` is authoritative**
(Python subprocesses do not reliably read `~/.ssh/config` on Windows). Always verify with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\kallon-hub-ssh-verify.ps1 -HubHost 18.220.75.237
```

**Permission denied?** Usually one of:

| Symptom | Fix |
|---------|-----|
| `UNPROTECTED PRIVATE KEY FILE` | Re-run install script or manual icacls steps (user-only ACL on `.pem`) |
| Plain `ssh ubuntu@…` fails but `-i …\terra-hub-ops.pem` works | Remove `Host *` `IdentityFile` from `~/.ssh/config` |
| Hub-provisioner fails, interactive ssh works | Set `$env:KALLON_OPS_SSH_IDENTITY_FILE` to the **`.pem`**, not `.pub` |
| Env var set but file missing | Copy PEM to `C:\kallon\secrets\terra-hub-ops.pem` |

### 7.3 Enrollment API env (automated peer-add)

**Use `subprocess` from day 1** — the API SSHs to `{gateway_host}` from the registry
using the ops key above. No manual `kallon-gateway-add-peer.sh` per tower.

Prerequisites: **Git Bash** (for the add-peer script), **OpenSSH client**, ops key
configured as in §7.1–7.2.

Create `C:\kallon\config\enrollment-api.env` (adjust paths):

```powershell
New-Item -ItemType Directory -Force -Path C:\kallon\config | Out-Null

$repo = "C:\path\to\kallon-sentry\CODE"
$bash = "C:\Program Files\Git\bin\bash.exe"
$addPeer = "$repo\scripts\kallon-gateway-add-peer.sh"

@"
KALLON_REGISTRY=postgres
DATABASE_URL=postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon
KALLON_OPS_SSH_PUBKEY_FILE=C:\kallon\secrets\terra-hub-ops.pub
KALLON_OPS_SSH_IDENTITY_FILE=C:\kallon\secrets\terra-hub-ops.pem
KALLON_PEER_BACKEND=subprocess
KALLON_ADDPEER_CMD="$bash" "$addPeer" --gateway-host {gateway_host} --pubkey {pubkey} --vpn-ip {vpn_ip} --device-id {device_id} --ssh-user ubuntu
"@ | Set-Content C:\kallon\config\enrollment-api.env -Encoding UTF8

icacls C:\kallon\config\enrollment-api.env /inheritance:r /grant:r "Administrators:(F)" "SYSTEM:(F)"
```

| Variable | Production value |
|----------|------------------|
| `KALLON_PEER_BACKEND` | **`subprocess`** — never `noop` in prod |
| `KALLON_ADDPEER_CMD` | Invokes `kallon-gateway-add-peer.sh`; API fills `{gateway_host}` etc. from the registry |

> **Do not** follow `field-test-setup.md` §B5 “Add peer on hub” in production.
> That section exists only for Path B lab runs with `KALLON_PEER_BACKEND=noop`.

### Run enrollment API (manual test)

```powershell
cd C:\path\to\kallon-sentry\CODE\infra\enrollment-api
$env:KALLON_REGISTRY = "postgres"
$env:DATABASE_URL = "postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Verify:

```powershell
curl http://127.0.0.1:8000/healthz
```

**Production:** terminate TLS in front (Caddy/nginx). Towers use
`ENROLLMENT_URL=https://enroll.yourdomain.com/v1` — not cleartext over the internet.
See `infra/enrollment-api/deploy/Caddyfile.example`.

---

## 8. First customer + hub (`kallon-hub-provision`) — production step

This is where the **first customer org** enters the registry in production. One
command:

1. **Creates** `cust_lab` in Postgres (if missing) and allocates `10.50.0.0/24`
2. SSHs to the hub, runs `gateway-init`, installs ops pubkey
3. Writes hub endpoint/pubkey/alert URL; sets `status=active`
4. Emits `gateway_manifest_*.json`

You do **not** need a separate `create-customer` (§6) first.

```powershell
$env:KALLON_REGISTRY = "postgres"
$env:DATABASE_URL = "postgresql://kallon:YOUR_PASSWORD@127.0.0.1:5432/kallon"
$env:KALLON_OPS_SSH_PUBKEY_FILE = "C:\kallon\secrets\terra-hub-ops.pub"
$env:KALLON_OPS_SSH_IDENTITY_FILE = "C:\kallon\secrets\terra-hub-ops.pem"

python infra/hub-provisioner/cli.py cust_lab `
  --provider manual --host 18.220.75.237 --ssh-user ubuntu `
  --subnet 10.50.0.0/24 --display-name "Kallon Lab"
```

Your existing Lightsail box is **customer hub #1** on the production architecture —
not a throwaway lab. After this, `register-tower` (§12 / field-test §5) adds towers
to `cust_lab`.

**Additional customers** (same ops key, new subnet):

```powershell
python infra/hub-provisioner/cli.py cust_acme `
  --provider lightsail --region us-east-2 `
  --subnet 10.51.0.0/24 --display-name "Acme Security"
```

Do **not** rely on the registry default without `DATABASE_URL` set.

---

## 9. Backups

Roadmap requires daily `pg_dump`. Example one-liner:

```powershell
$backupDir = "C:\kallon\backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

$env:PGPASSWORD = "YOUR_PASSWORD"
& "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" `
  -U kallon -h localhost -d kallon -Fc `
  -f "$backupDir\kallon_$(Get-Date -Format yyyyMMdd).dump"
```

Schedule in **Task Scheduler** (e.g. daily 02:00). Copy dumps off-site.

---

## 10. Laptop lab vs Windows Server

| Context | Registry backend |
|---------|------------------|
| Quick laptop tests (Path A/B in field-test guide) | SQLite: `$env:KALLON_REGISTRY="sqlite"` |
| Windows Server control plane (this doc) | Postgres + `DATABASE_URL` |

Use Postgres on the server when enrollment API, hub provisioner, and factory CLI
must share one durable registry.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `DATABASE_URL not set` | Env var missing | Set `$env:DATABASE_URL` or use `--registry sqlite` on laptop only |
| `psycopg is not installed` | Missing pip package | `pip install -r registry/requirements.txt` |
| `connection refused` | Service down or wrong host | `Get-Service postgresql-x64-16`; check `listen_addresses` |
| `password authentication failed` | Wrong password in URL | Reset: `ALTER USER kallon PASSWORD '...';` in psql as postgres |
| `permission denied for table` | Schema owned by wrong user | Re-run init as `kallon` owner; check `GRANT` on database |
| Enrollment API can't reach DB from another host | `pg_hba` / firewall | Add host rule for API IP only; never open 5432 publicly |

---

## 12. Day-1 production automation (full checklist)

**One-time (control plane — Windows Server):**

- [ ] Postgres installed; `listen_addresses = 'localhost'`; no public 5432
- [ ] `init-schema` + customer row (`create-customer` or hub provisioner)
- [ ] Customer hub active in registry (`gateway_endpoint`, `gateway_public_key`, `status=active`)
- [ ] `enrollment-api.env` with `KALLON_PEER_BACKEND=subprocess` + `KALLON_ADDPEER_CMD`
- [ ] SSH from API service account → hub works (PEM key, no password)
- [ ] Enrollment API running on `127.0.0.1:8000` (Windows service or NSSM)
- [ ] TLS reverse proxy on `:443` → `ENROLLMENT_URL=https://enroll.yourdomain.com/v1`
- [ ] Hub alert listener as **systemd** on VPS (not `nohup`)

**Per tower (factory — then zero touch in field):**

- [ ] `register-tower` in registry → save `ENROLLMENT_TOKEN` once
- [ ] Bake `/etc/kallon/device.env` (`DEVICE_ID`, `ENROLLMENT_URL`, `ENROLLMENT_TOKEN`, camera vars)
- [ ] Copy hub `alert.key` to tower (must match hub HMAC secret)
- [ ] `kallon-jetson-install.sh` on Jetson
- [ ] Enable `kallon-enroll.service` (first boot auto-enrolls)

**Skip in production:**

- Path B laptop SQLite + API on `192.168.1.230`
- `KALLON_PEER_BACKEND=noop`
- Manual `kallon-gateway-add-peer.sh` after each enroll (`field-test-setup.md` §B5)

**Automated enroll flow (no manual peer step):**

```text
Tower boot → kallon-enroll.service
  → POST /v1/enroll (HTTPS)
  → API: Postgres allocate IP + SSH add-peer on hub (terra-hub-ops.pem)
  → Jetson: wg0 up → handshake → POST /v1/enroll/confirm → .enrolled
```

Verify after first enroll (same check at 1 hub or 100):

```powershell
ssh ubuntu@18.220.75.237 "sudo wg show wg0"
# New peer with tower vpn_ip/32 — no per-tower SSH setup
```

**Note:** `deploy/kallon-enroll.service.example` points at
`/opt/kallon/scripts/kallon-enroll.sh`, but `70-app.sh` does not copy `scripts/`
to `/opt/kallon`. Either set `ExecStart` to your repo path
(e.g. `/home/khalifa/kallon/scripts/kallon-enroll.sh`) or copy `scripts/` into
`/opt/kallon` during install.

---

## 13. Checklist (Postgres only)

- [ ] PostgreSQL 16 installed and service running
- [ ] User `kallon` and database `kallon` created
- [ ] `listen_addresses` and `pg_hba.conf` locked down (no public 5432)
- [ ] `python -m registry.cli init-schema` succeeded
- [ ] `create-customer` smoke test passed
- [ ] `DATABASE_URL` stored in restricted env file (not committed to git)
- [ ] Enrollment API `healthz` OK against Postgres-backed registry
- [ ] `KALLON_PEER_BACKEND=subprocess` and peer-add SSH tested
- [ ] Daily `pg_dump` scheduled

---

*Terra Industries · Kallon Sentry Tower · Postgres Windows Server Setup · June 2026*
