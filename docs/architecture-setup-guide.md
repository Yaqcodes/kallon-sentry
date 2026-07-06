# Kallon architecture & setup guide

**Terra Industries · Internal Engineering**

End-to-end walkthrough from **empty control plane** to **tower enrolled and streaming
over VPN** — organized by **nodes** in the architecture, with **commands**, **what
each command touches**, and **diagrams** at every layer.

| Related | Role |
|---------|------|
| **`docs/README.md`** | Documentation index |
| `docs/postgres-windows-server-setup.md` | Path P detail (Postgres, API, Caddy, hub §8) |
| `docs/order-fulfillment.md` | `kallon-fulfill-order` + business walkthrough |
| `docs/field-test-setup.md` | Bench validation & module reference |
| `docs/alert-webhook.md` | Dashboard contract (RTSP + HMAC) |

### Conventions (read first)

Commands below use **variables** — set once per session on Artemis. Replace
with your real IPs; examples in parentheses are Terra lab bench values only.

```powershell
$HUB_HOST     = "YOUR_HUB_PUBLIC_IP"    # e.g. 18.220.75.237 — Lightsail / VPS public IP
$JETSON_HOST  = "YOUR_JETSON_LAN_IP"    # e.g. 192.168.1.246 — tower on factory Wi‑Fi
$HUB_SSH_USER = "ubuntu"                # SSH user on hub VPS
$JETSON_USER  = "khalifa"               # SSH user on Jetson
$PEM          = "C:\kallon\secrets\terra-hub-ops.pem"
$FACTORY_DIR  = "C:\kallon\factory\lab" # fulfill-order --output-dir
```

| Variable | What it is |
|----------|------------|
| `$HUB_HOST` | **Public IP** of the customer hub (WireGuard endpoint, SSH target) |
| `$JETSON_HOST` | **LAN IP** of the tower you are provisioning at the bench |

---

## 1. The nodes (who lives where)

```mermaid
flowchart TB
    subgraph INTERNET["Internet"]
        DNS["DNS enroll.*"]
        TOWER_WAN["Customer WAN\nWi‑Fi / LTE"]
    end

    subgraph CP["Control plane — Artemis (Windows Server)"]
        PG[("PostgreSQL\n127.0.0.1:5432")]
        API["Enrollment API\nuvicorn :8000"]
        OPS["Ops CLI\nfulfill-order · hub-provision · registry"]
        CADDY["TLS edge\nCaddy :443\n(or Cloudflare Tunnel)"]
        PEM["terra-hub-ops.pem"]
    end

    subgraph HUB["Customer hub — one Ubuntu VPS per org"]
        WGH["WireGuard hub\nUDP :51820"]
        AL["Alert listener\nHTTP :8080 wg0 only"]
        WGCONF["/etc/wireguard/wg0.conf"]
    end

    subgraph TOWER["Sentry Tower — Jetson"]
        ENV["/etc/kallon/device.env"]
        ENR["kallon-enroll.sh"]
        MTX["mediamtx :8554"]
        WGJ["WireGuard wg0"]
    end

    TOWER_WAN --> DNS
    DNS --> CADDY
    CADDY --> API
    API --> PG
    OPS --> PG
    API --> PEM
    PEM -->|SSH :22| HUB
    OPS -->|SSH :22| HUB
    ENR -->|HTTPS POST /v1/enroll| CADDY
    WGJ <-->|UDP 51820 outbound| WGH
    MTX --> WGJ
```

### Node cheat sheet

| Node | Hostname / example | Runs | Never exposed to internet |
|------|-------------------|------|---------------------------|
| **Control plane** | Artemis (e.g. `192.168.1.236` LAN) | Postgres, enrollment API, ops automation | Postgres `:5432` |
| **Customer hub** | `$HUB_HOST` (public IP) | WireGuard hub, alert listener | `:8080` (VPN only) |
| **Sentry tower** | Jetson on site | mediamtx, watchdog, enroll client | Camera VLAN |
| **Public edge** | `enroll.yourdomain.com` | HTTPS only | — |

---

## 2. Layers (build order)

Build **bottom-up**: data → automation → public edge → customer → field.

```text
┌─────────────────────────────────────────────────────────────────┐
│ L4  FIELD        SCP device.env → Jetson install → enroll        │
├─────────────────────────────────────────────────────────────────┤
│ L3  ORDER        fulfill-order (hub + towers + device_*.env)   │
├─────────────────────────────────────────────────────────────────┤
│ L2  PUBLIC API   NSSM service → HTTPS URL (Caddy / tunnel/ngrok)│
├─────────────────────────────────────────────────────────────────┤
│ L1  AUTOMATION   SSH ops key → enrollment-api.env                │
├─────────────────────────────────────────────────────────────────┤
│ L0  DATA         PostgreSQL → init-schema → registry tables      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Registry data model (what Postgres holds)

```mermaid
erDiagram
    customers ||--o{ towers : has
    customers ||--o{ ip_allocations : owns
    customers {
        string customer_id "cust_acme"
        string vpn_subnet "10.51.0.0/24"
        string gateway_endpoint "203.0.113.1:51820"
        string gateway_public_key "hub WG pubkey"
        string status "active"
    }
    towers {
        string device_id "kln_acme_000001"
        string enrollment_token_hash "sha256"
        string claim_code "clm_..."
        string vpn_ip "10.51.0.2"
        string status "registered|enrolled|active"
    }
```

**Rule:** Towers and buyers **never** connect to Postgres. Only ops CLI and the
enrollment API use `DATABASE_URL`.

---

## Phase 0 — Prerequisites

**Where:** Artemis + your laptop (for git).

| Requirement | Node | Purpose |
|-------------|------|---------|
| Windows Server (Artemis) | Control plane | Postgres + API + ops |
| Python 3.10+ | Control plane | registry, API, fulfillment |
| Git + repo `field-test` | Control plane | scripts & CLI |
| Git Bash + OpenSSH | Control plane | `kallon-gateway-add-peer.sh`, hub SSH |
| AWS creds (optional) | Control plane | Lightsail hub create |
| Hub VPS or Lightsail | Hub | WireGuard + alerts |
| Jetson (factory/site) | Tower | hardware under test |

```powershell
cd C:\Users\Artemis\Documents\kallon-sentry
git fetch origin
git checkout field-test
git pull origin field-test

pip install -r requirements.txt
pip install -r registry/requirements.txt
pip install -r infra/enrollment-api/requirements.txt
```

| Command | Invokes | Touches |
|---------|---------|---------|
| `git pull` | git | local repo |
| `pip install …` | pip | Python packages |

### Windows notes (Artemis)

**Run CLIs as modules** from the repo root (direct file paths break relative imports):

```powershell
python -m infra.fulfillment.cli …      # kallon-fulfill-order
python -m infra.hub-provisioner.cli …  # kallon-hub-provision
python -m registry.cli …
```

**Load ops env** (`enrollment-api.env` is unsigned — bypass execution policy for this session):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
. .\scripts\load-control-plane.ps1
```

`load-control-plane.ps1` loads Postgres + SSH vars from `C:\kallon\config\enrollment-api.env`.
It does **not** set `KALLON_ENROLLMENT_URL` — set that separately before fulfill-order.

---

## Phase 1 (L0) — PostgreSQL on Artemis

**Goal:** Registry database listening on localhost only.

```mermaid
flowchart LR
    OPS["Ops PowerShell"] -->|psql / registry.cli| PG[("PostgreSQL\n127.0.0.1:5432")]
```

### Commands

```powershell
# After EDB installer — create app user (psql as postgres superuser)
CREATE USER kallon WITH PASSWORD '…';
CREATE DATABASE kallon OWNER kallon;

$env:KALLON_REGISTRY = "postgres"
$env:DATABASE_URL = "postgresql://kallon:PASSWORD@127.0.0.1:5432/kallon"

python -m registry.cli init-schema
psql -U kallon -h localhost -d kallon -c "\dt"
```

| Command | Invokes | Resources touched |
|---------|---------|-------------------|
| `CREATE USER/DATABASE` | psql | Postgres system catalogs |
| `init-schema` | `registry/cli.py` → `postgres_provider.py` | Creates `customers`, `towers`, `ip_allocations`, `audit_events` |
| `\dt` | psql | lists tables |

**Verify:** `{"ok": true, "action": "init-schema"}` and four tables exist.

**Security:** `listen_addresses = 'localhost'` in `postgresql.conf`; do **not**
forward TCP 5432 on Starlink/router.

---

## Phase 2 (L1a) — Ops SSH identity

**Goal:** One PEM for all hub SSH (provision + enroll peer-add).

```mermaid
flowchart LR
    OPS["Control plane"] -->|SSH -i terra-hub-ops.pem| HUB["Hub VPS :22"]
    PEM["C:\\kallon\\secrets\\terra-hub-ops.pem"]
    OPS --> PEM
```

### Commands

```powershell
# Copy existing PEM or generate new
powershell -ExecutionPolicy Bypass -File .\scripts\install-terra-hub-ops-key.ps1 -SourcePem C:\path\to\key.pem

$env:KALLON_OPS_SSH_IDENTITY_FILE = "C:\kallon\secrets\terra-hub-ops.pem"
$env:KALLON_OPS_SSH_PUBKEY_FILE   = "C:\kallon\secrets\terra-hub-ops.pub"

powershell -ExecutionPolicy Bypass -File .\scripts\kallon-hub-ssh-verify.ps1 -HubHost $HUB_HOST
```

| Command | Invokes | Resources touched |
|---------|---------|-------------------|
| `install-terra-hub-ops-key.ps1` | icacls, copy | `C:\kallon\secrets\` |
| `kallon-hub-ssh-verify.ps1` | ssh | hub `:22`, `wg show` |

**Verify:** Test 1 passes (SSH with explicit `-i`).

---

## Phase 3 (L1b) — Enrollment API config

**Goal:** API can read registry and SSH-add peers on enroll.

```mermaid
flowchart TB
    API["Enrollment API"]
    API -->|DATABASE_URL| PG[("Postgres")]
    API -->|KALLON_ADDPEER_CMD| BASH["Git Bash"]
    BASH -->|SSH| ADDPEER["kallon-gateway-add-peer.sh"]
    ADDPEER -->|SSH| HUB["Hub wg0.conf"]
```

### Commands

Create `C:\kallon\config\enrollment-api.env`:

```powershell
New-Item -ItemType Directory -Force -Path C:\kallon\config | Out-Null

$repo = "C:\Users\Artemis\Documents\kallon-sentry"
$bash = "C:\Program Files\Git\bin\bash.exe"
$addPeer = "$repo\scripts\kallon-gateway-add-peer.sh"

@"
KALLON_REGISTRY=postgres
DATABASE_URL=postgresql://kallon:PASSWORD@127.0.0.1:5432/kallon
KALLON_OPS_SSH_PUBKEY_FILE=C:\kallon\secrets\terra-hub-ops.pub
KALLON_OPS_SSH_IDENTITY_FILE=C:\kallon\secrets\terra-hub-ops.pem
KALLON_PEER_BACKEND=subprocess
KALLON_ADDPEER_CMD="$bash" "$addPeer" --gateway-host {gateway_host} --pubkey {pubkey} --vpn-ip {vpn_ip} --device-id {device_id} --ssh-user ubuntu
"@ | Set-Content C:\kallon\config\enrollment-api.env -Encoding UTF8
```

Load in session (see **Windows notes** above for execution policy):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
. .\scripts\load-control-plane.ps1
```

| File / variable | Consumed by |
|-----------------|-------------|
| `enrollment-api.env` | NSSM service, `load-control-plane.ps1` |
| `KALLON_PEER_BACKEND=subprocess` | `infra/enrollment-api/app/peering.py` |
| `KALLON_ADDPEER_CMD` | `SubprocessPeerAdder.add_peer()` on each enroll |
| `KALLON_ENROLLMENT_URL` | **fulfill-order only** (not in API service env by default) |

---

## Phase 4 (L2a) — Enrollment API service (NSSM)

**Goal:** uvicorn runs 24/7 on loopback.

```mermaid
flowchart LR
    NSSM["Windows service\nkallon-enrollment-api"] -->|127.0.0.1:8000| API["FastAPI app.main"]
    API --> PG[("Postgres")]
```

### Commands

```powershell
$nssm = "C:\kallon\nssm.exe"
$python = (Get-Command python).Source
$repo = "C:\Users\Artemis\Documents\kallon-sentry"

& $nssm install kallon-enrollment-api $python "-m" "uvicorn" "app.main:app" "--host" "127.0.0.1" "--port" "8000"
& $nssm set kallon-enrollment-api AppDirectory "$repo\infra\enrollment-api"
# Set ALL vars from enrollment-api.env via AppEnvironmentExtra or NSSM GUI

& $nssm start kallon-enrollment-api
curl http://127.0.0.1:8000/healthz
```

| Command | Invokes | Resources touched |
|---------|---------|-------------------|
| `nssm install/start` | NSSM | Windows Service Control Manager |
| uvicorn | `infra/enrollment-api/app/main.py` | `:8000`, Postgres on enroll |
| `GET /healthz` | FastAPI | returns `{"status":"ok"}` |

**Endpoints (tower-facing):**

| Method | Path | Handler action |
|--------|------|----------------|
| GET | `/healthz` | liveness |
| POST | `/v1/enroll` | validate token → allocate IP → **add_peer** → return WG config |
| POST | `/v1/enroll/confirm` | mark tower `active` |

---

## Phase 5 (L2b) — Public HTTPS (pick one path)

Towers need a **stable HTTPS URL**. Postgres stays private.

### Option A — Caddy on Artemis (direct)

```text
Tower ──HTTPS──► DNS ──► Starlink/router ──► Caddy :443 ──► 127.0.0.1:8000
```

Requires: DNS A record, **port forward 80+443** (often hard on Starlink).

```text
# C:\caddy\Caddyfile
kallon-enroll.duckdns.org {
    reverse_proxy 127.0.0.1:8000
}
```

```powershell
cd C:\caddy
.\caddy.exe run --config C:\caddy\Caddyfile
# then: caddy install / caddy start
```

### Option B — Cloudflare Tunnel (recommended on Starlink)

```text
Tower ──HTTPS──► Cloudflare edge ──tunnel(outbound)──► Artemis :8000
```

**Why no port forwarding:** `cloudflared` on Artemis opens **outbound** connections to
Cloudflare (like browsing the web). Traffic returns through that tunnel. Starlink never
needs inbound TCP 80/443 to your home IP.

**What changes:** public hostname + `KALLON_ENROLLMENT_URL`. Postgres, NSSM API, SSH
peer-add unchanged. **Skip Caddy.**

Rough steps:

1. Cloudflare account + domain (or use Cloudflare DNS for a domain you own).
2. Install `cloudflared` on Artemis.
3. Create tunnel → public hostname `enroll.yourdomain.com` → `http://127.0.0.1:8000`.
4. Run `cloudflared` as a service (always on).
5. `curl https://enroll.yourdomain.com/healthz` from LTE.

### Option C — ngrok (bench only)

```text
Tower ──HTTPS──► ngrok URL ──► 127.0.0.1:8000
```

**What changes:** only `KALLON_ENROLLMENT_URL` and `ENROLLMENT_URL` in `device.env`.
Postgres, NSSM, hub provision, Jetson install flow unchanged. **Skip Caddy.**

**Steps on Artemis** (API must be running on `:8000`):

```powershell
# One-time: ngrok config add-authtoken YOUR_TOKEN
ngrok http 8000
# Paid reserved hostname (stable URL):
# ngrok http 8000 --domain=kallon-enroll.ngrok.app

curl https://YOUR-SUBDOMAIN.ngrok-free.app/healthz

$env:KALLON_ENROLLMENT_URL = "https://YOUR-SUBDOMAIN.ngrok-free.app/v1"
```

| ngrok tier | URL stability | Use for |
|------------|---------------|---------|
| Free | **Changes** every restart | Quick test; edit Jetson `ENROLLMENT_URL` each time |
| Paid reserved | **Stable** | Bench / small pilot |

`ngrok` must stay running while towers enroll. Towers never talk to Postgres or Artemis
directly — only the ngrok HTTPS URL.

### Option D — Railway / VPS (API + Postgres in cloud)

```text
Tower ──HTTPS──► Railway app ──► Railway Postgres
Ops ──DATABASE_URL──► same Postgres
```

Move `DATABASE_URL` and deploy `infra/enrollment-api`; ops runs from laptop.

---

Set enrollment URL for all factory output:

```powershell
$env:KALLON_ENROLLMENT_URL = "https://kallon-enroll.duckdns.org/v1"
```

| Component | Internet-visible? |
|-----------|-------------------|
| `https://enroll.*/v1` | **Yes** |
| Postgres `:5432` | **No** |
| uvicorn `:8000` | **No** (loopback or tunnel target) |

**Verify:** `curl https://YOUR-ENROLL-HOST/healthz` from **LTE** (not same Wi‑Fi).

---

## Phase 6 (L3) — Customer, hub, towers, factory files

**Goal:** `cust_*` active, hub provisioned (if needed), towers registered, **`device_*.env`
written on Artemis**. One command does hub + towers unless hub is already `active`.

```mermaid
sequenceDiagram
    participant OPS as Ops (Artemis)
    participant FO as fulfill-order
    participant PG as Postgres
    participant HUB as Hub VPS

    OPS->>FO: python -m infra.fulfillment.cli
    FO->>PG: create/update customer
    alt hub not active
        FO->>HUB: SSH gateway-init.sh
        HUB-->>FO: gateway_manifest.json
        FO->>PG: gateway_endpoint, status=active
    end
    FO->>PG: register-tower × N
    FO->>FO: write device_*.env + fulfillment_*.json
```

### Fulfill-order — pick hub provider

**One command** creates customer (if new), provisions hub (if not `active`), registers
towers, and writes `device_*.env` on Artemis. Set vars from **Conventions** first.

```powershell
cd C:\Users\Artemis\Documents\kallon-sentry

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
. .\scripts\load-control-plane.ps1

$env:KALLON_ENROLLMENT_URL = "https://kallon-enroll.duckdns.org/v1"
# or ngrok: $env:KALLON_ENROLLMENT_URL = "https://YOUR.ngrok.app/v1"
```

| Provider | When to use | Hub |
|----------|-------------|-----|
| **`manual`** | Hub **already exists** (lab Lightsail, enterprise on-prem, any Ubuntu you SSH to) | You pass `--host $HUB_HOST` |
| **`lightsail`** | **New retail customer** — CLI creates a fresh AWS Lightsail VPS | boto3 + AWS creds; no `--host` |

#### `manual` — existing hub (lab / first production hub)

Use for Terra lab on an existing Lightsail box, or any hub that already has (or will
receive) `gateway-init.sh` over SSH.

```powershell
$HUB_HOST = "YOUR_HUB_PUBLIC_IP"   # lab example: 18.220.75.237
$FACTORY_DIR = "C:\kallon\factory\lab"

python -m infra.fulfillment.cli lab `
  --display-name "Kallon Lab" `
  --provider manual --host $HUB_HOST `
  --towers 1 --cameras 2 `
  --subnet 10.50.0.0/24 `
  --output-dir $FACTORY_DIR
```

Hub-only (no towers yet):

```powershell
python -m infra.hub-provisioner.cli cust_lab `
  --provider manual --host $HUB_HOST `
  --subnet 10.50.0.0/24 --display-name "Kallon Lab"
```

#### `lightsail` — new VPS per customer (retail)

Use when onboarding a **new** customer org and you want the CLI to **create** the hub VM.

```powershell
$FACTORY_DIR = "C:\kallon\factory\acme"

python -m infra.fulfillment.cli acme `
  --display-name "Acme Security" `
  --provider lightsail --region us-east-2 `
  --towers 3 --cameras 2 `
  --output-dir $FACTORY_DIR
```

Subnet auto-assigns (`10.50`, `10.51`, …) unless you pass `--subnet`. After success,
read the new hub public IP from registry or provisioner output and set `$HUB_HOST` for
verify/SCP steps below.

Hub-only:

```powershell
python -m infra.hub-provisioner.cli cust_acme `
  --provider lightsail --region us-east-2 `
  --display-name "Acme Security"
```

`ENROLLMENT_URL` inside each `device_*.env` is set from `$env:KALLON_ENROLLMENT_URL`
(or `--enrollment-url`). Required unless `--dry-run`.

**Hub VPN peer forwarding:** `kallon-gateway-init.sh` (run by hub-provisioner) sets
`ufw route allow in on wg0 out on wg0` so NOC/dashboard peers reach tower RTSP.
Hubs provisioned before this rule: run `kallon-gateway-ensure-forwarding.sh` once on
the **hub VPS** — see `docs/postgres-windows-server-setup.md` §8.1.

### What fulfill-order does / does not do

| Does | Does **not** |
|------|----------------|
| Create `cust_*` + subnet if new | SSH or SCP to **Jetson** |
| Hub-provision if not `active` | Run `kallon-jetson-install.sh` |
| SSH → hub (`gateway-init.sh`) | Fetch or copy `alert.key` to Jetson (factory step below) |
| `register-tower` in Postgres | Start enrollment on tower |
| Write `device_kln_*.env` on Artemis | |
| Write `fulfillment_*.json` (secrets) | |

### Files written on Artemis (`--output-dir`)

| File | Copy to Jetson? | Purpose |
|------|-----------------|---------|
| **`device_kln_<slug>_00000N.env`** | **Yes** → `/etc/kallon/device.env` | Per-tower identity, `ENROLLMENT_URL`, token |
| **`alert.key`** | **Yes** → `/etc/kallon/alert.key` | **Not** written by fulfill-order — fetch from hub (below) |
| `fulfillment_cust_<slug>.json` | **No** — ops secret | Plaintext tokens, QR payloads |

### `alert.key` — one per hub, same on every tower for that customer

HMAC alerts use a **shared secret** between the **hub** and **all towers** on that
customer hub. It is **not** per-Jetson and **not** one global key for all customers.

```text
cust_lab hub ($HUB_HOST)
  /etc/kallon/alert.key  ──same bytes──►  tower 1, tower 2, tower 3 …

cust_acme hub (different $HUB_HOST)
  different alert.key  ──►  Acme towers only
```

`kallon-gateway-init.sh` creates `alert.key` on the **hub** at provision time.
fulfill-order does **not** embed it in `device_*.env`. Factory ops must **pull it
from the hub** into the factory bundle, then ship it to **each** tower with that
tower's `device.env`.

**Step A — fetch hub key into factory dir** (once per customer hub, after fulfill-order):

```powershell
# Saves one alert.key per customer — reuse for every tower on that hub
ssh -i $PEM "${HUB_SSH_USER}@${HUB_HOST}" "sudo cat /etc/kallon/alert.key" `
  > "$FACTORY_DIR\alert.key"
```

**Step B — copy to each tower with its `device_*.env`** (Phase 7). Every tower on
`cust_lab` gets the **same** `$FACTORY_DIR\alert.key` plus its own `device_*.env`.

`kallon-fulfill-order` writes `device_*.env` with **Unix LF** line endings even on
Windows (`write_factory_file`). If you still see `$'\r': command not found` on the
Jetson when running the installer, the file has Windows CRLF line endings — run
`sudo sed -i 's/\r$//' /etc/kallon/device.env` (and the same for `alert.key` if
needed). See `docs/identity-and-secrets.md` §3.2.

| Command | Invokes | Resources touched |
|---------|---------|-------------------|
| `infra.fulfillment.cli` | hub-provisioner + `register-tower` + `device_env.py` | Postgres, hub SSH, local files |
| `kallon-gateway-init.sh` (remote) | wireguard, ufw, systemd | hub `wg0`, `alert.key`, `:51820`, **wg0 peer forwarding** |

**`run_gateway_init()` copies to hub only:**

| File | Purpose |
|------|---------|
| `scripts/kallon-gateway-init.sh` | hub bring-up (includes `ufw route allow in on wg0 out on wg0`) |
| `scripts/kallon-gateway-ensure-forwarding.sh` | re-applied after `wg0` is up; idempotent legacy migration |
| `infra/hub/alert_listener.py` | HMAC alert receiver |
| `terra-hub-ops.pub` | ops SSH access |

**Verify:**

```powershell
python -m registry.cli list-customers
python -m registry.cli list-towers --customer cust_lab
Get-Content "$FACTORY_DIR\device_kln_lab_000001.env" | Select-String ENROLLMENT_URL
Test-Path "$FACTORY_DIR\alert.key"   # after Step A above
ssh -i $PEM "${HUB_SSH_USER}@${HUB_HOST}" "sudo wg show wg0"
```

Registry row after register:

| Field | Example |
|-------|---------|
| `device_id` | `kln_lab_000001` |
| `enrollment_token_hash` | sha256 of `enr_…` |
| `claim_code` | `clm_…` |
| `status` | `registered` (→ `active` after enroll) |

---

## Phase 7 (L4) — Copy to Jetson & factory install

**Where:** Any machine that can SSH to the Jetson (Artemis **or** your laptop on the
same LAN). fulfill-order does not contact the Jetson.

**Factory bundle per tower** (two files from Artemis, always together):

| File on Artemis | Jetson path |
|-----------------|-------------|
| `device_kln_<slug>_00000N.env` | `/etc/kallon/device.env` |
| `alert.key` (from hub — same for all towers on that hub) | `/etc/kallon/alert.key` |

```mermaid
flowchart LR
    HUB["Hub $HUB_HOST\nalert.key"] -->|ssh cat once| ART["$FACTORY_DIR"]
    FO["fulfill-order"] -->|device_*.env| ART
    ART -->|"SCP both files"| JET["Jetson $JETSON_HOST\n/etc/kallon/"]
```

```mermaid
flowchart TB
    subgraph JETSON["Jetson"]
        INSTALL["kallon-jetson-install.sh\nmodules 00–99"]
        INSTALL --> MTX["mediamtx"]
        INSTALL --> WD["kallon-watchdog"]
        INSTALL --> FW["iptables :8554 wg0 only"]
        DE["/etc/kallon/device.env"]
        DE --> INSTALL
    end
    HUBKEY["hub alert.key"] -->|copy| AK["/etc/kallon/alert.key"]
```

### Copy factory bundle to Jetson

Prerequisite: `$FACTORY_DIR\alert.key` exists (Phase 6 Step A). Use `$JETSON_HOST`
from **Conventions**.

```powershell
$JETSON_HOST = "YOUR_JETSON_LAN_IP"

# Both files — every tower gets its own device.env + the same alert.key for that hub
scp "$FACTORY_DIR\device_kln_lab_000001.env" "${JETSON_USER}@${JETSON_HOST}:/tmp/"
scp "$FACTORY_DIR\alert.key"                 "${JETSON_USER}@${JETSON_HOST}:/tmp/"
ssh "${JETSON_USER}@${JETSON_HOST}"
```

### Commands on Jetson

Before install, on each Dahua camera: for reliable kiosk video, set substream to
H.264 in the camera web UI when you provision them (**Setup → Camera → Video →
Encode** → substream).

```bash
RUNTIME_USER="${SUDO_USER:-$(logname 2>/dev/null || id -un)}"
sudo install -d -m 0750 -o root -g "$RUNTIME_USER" /etc/kallon
sudo install -m 0640 -o root -g "$RUNTIME_USER" /tmp/device_kln_lab_000001.env /etc/kallon/device.env
sudo sed -i 's/\r$//' /etc/kallon/device.env
sudo install -m 0640 -o root -g "$RUNTIME_USER" /tmp/alert.key /etc/kallon/alert.key
sudo sed -i 's/\r$//' /etc/kallon/alert.key

cd /path/to/kallon-repo
sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env

sudo cp deploy/kallon-enroll.service.example /etc/systemd/system/kallon-enroll.service
sudo systemctl daemon-reload
sudo systemctl enable kallon-enroll.service

sudo scripts/kallon-acceptance.sh --env /etc/kallon/device.env
```

| Command | Invokes | Resources touched |
|---------|---------|-------------------|
| `kallon-jetson-install.sh` | modules 00–99 | network, mediamtx, wg, systemd |
| `kallon-acceptance.sh` | local checks | camera route, RTSP, HMAC dry-run |
| `kallon-enroll.service` | `kallon-enroll.sh` at boot | skipped if `.enrolled` exists |

---

## Phase 8 (L4) — First-boot enrollment (automatic)

**Where:** Customer site (or factory WAN). **No ops CMD.**

```mermaid
sequenceDiagram
    participant T as Jetson
    participant API as Enrollment API
    participant PG as Postgres
    participant H as Hub VPS

    T->>T: kallon-wg-provision --print-pubkey
    T->>API: POST /v1/enroll
    API->>PG: get_tower, allocate_ip
    API->>H: SSH add-peer
    API-->>T: vpn_ip, gateway_*, confirm_token
    T->>T: wg0 up
    T->>H: WireGuard handshake
    T->>API: POST /v1/enroll/confirm
    API->>PG: status=active
    T->>T: touch .enrolled
```

### Manual trigger (bench)

```bash
sudo rm -f /etc/kallon/.enrolled
sudo scripts/kallon-enroll.sh --env /etc/kallon/device.env
```

| Step | Script / service | Calls |
|------|------------------|-------|
| 1 | `kallon-enroll.sh` | `kallon-wg-provision.sh --print-pubkey` |
| 2 | curl | `POST $ENROLLMENT_URL/enroll` |
| 3 | API | Postgres + `kallon-gateway-add-peer.sh` → hub SSH |
| 4 | `kallon-wg-provision.sh` | writes `wg0.conf`, starts wg |
| 5 | curl | `POST $ENROLLMENT_URL/enroll/confirm` |

**Verify on control plane:**

```powershell
python -m registry.cli get-config --device kln_lab_000001
ssh -i $PEM "${HUB_SSH_USER}@${HUB_HOST}" "sudo wg show wg0"
```

Expected: tower `status=active`, hub shows peer with `/32`.

---

## Phase 9 — Steady state (working system)

```mermaid
flowchart LR
    CAM["Camera RTSP :554"] --> MTX["mediamtx :8554"]
    MTX --> WG["wg0"]
    WG <-->|VPN| HUB["Hub"]
    WD["watchdog"] -->|HMAC POST| AL["hub :8080/alerts"]
    NOC["NOC / Dashboard"] -->|RTSP tcp| WG
```

### Verification commands

```bash
# On Jetson
sudo wg show wg0
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1

# From VPN peer (NOC laptop — not the hub shell)
# Windows: Test-NetConnection <tower-vpn-ip> -Port 8554  → TcpTestSucceeded : True
ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1
```

**Hub requirement:** NOC → tower RTSP is routed **through** the hub. Fresh hubs get
`ufw route allow in on wg0 out on wg0` from `kallon-gateway-init.sh`. Hubs provisioned
earlier: run `kallon-gateway-ensure-forwarding.sh` on the **hub VPS** once (see
`docs/postgres-windows-server-setup.md` §8.1). VLC/ffprobe from the hub (`10.50.0.1`)
can work while the NOC peer fails if this rule is missing.

| Path | Protocol | Address |
|------|----------|---------|
| Live video | RTSP/TCP over VPN | `rtsp://<tower-vpn-ip>:8554/cam1` |
| Alerts | HMAC POST over VPN | `http://<hub-vpn-ip>:8080/alerts` |
| Enrollment | HTTPS (once) | `POST https://enroll.*/v1/enroll` |

---

## Master command → resource matrix

| Command | Run on | Reads | Writes | Network calls |
|---------|--------|-------|--------|---------------|
| `registry.cli init-schema` | Artemis | — | Postgres DDL | localhost:5432 |
| `registry.cli list-towers` | Artemis | Postgres | — | localhost:5432 |
| `python -m infra.hub-provisioner.cli` | Artemis | Postgres | Postgres, hub disk | SSH→hub:22 |
| `python -m infra.fulfillment.cli` | Artemis | Postgres | Postgres, `device_*.env` | may SSH/AWS |
| `uvicorn app.main` | Artemis | Postgres | Postgres, hub via SSH | listen :8000 |
| `POST /v1/enroll` | Artemis (API) | Postgres | Postgres, hub `wg0` | inbound HTTPS |
| `kallon-jetson-install.sh` | Jetson | `device.env` | local systemd, mediamtx | camera LAN |
| `kallon-enroll.sh` | Jetson | `device.env` | `wg0.conf`, `.enrolled` | HTTPS→enroll URL |
| `kallon-acceptance.sh` | Jetson | local services | — | camera, optional VPN |

---

## Setup checklist (printable)

### Control plane (Artemis)

- [ ] PostgreSQL 16 installed, `kallon` DB + user
- [ ] `python -m registry.cli init-schema` OK
- [ ] `terra-hub-ops.pem` installed, SSH verify passes
- [ ] `C:\kallon\config\enrollment-api.env` complete (`subprocess` peer backend)
- [ ] NSSM `kallon-enrollment-api` → `curl http://127.0.0.1:8000/healthz`
- [ ] Public HTTPS (Caddy / tunnel / ngrok reserved / Railway)
- [ ] `KALLON_ENROLLMENT_URL` set

### Customer + hub + factory files (Phase 6)

- [ ] `python -m infra.fulfillment.cli …` → customer `status=active`
- [ ] `$FACTORY_DIR\device_*.env` exists with correct `ENROLLMENT_URL`
- [ ] Hub `wg show wg0` OK on `$HUB_HOST`
- [ ] `$FACTORY_DIR\alert.key` fetched from hub (one per customer hub)

### Tower (Phases 7–8)

- [ ] SCP **both** `device_*.env` + `alert.key` → Jetson `/etc/kallon/`
- [ ] Same `alert.key` on every tower for that hub
- [ ] `kallon-jetson-install.sh` + acceptance PASS
- [ ] `kallon-enroll.service` enabled
- [ ] Enroll → registry `active`, peer on hub
- [ ] `ffprobe rtsp://<vpn-ip>:8554/cam1` from VPN peer

---

## Common failure map

| Symptom | Layer | Check |
|---------|-------|-------|
| `init-schema` fails | L0 | Postgres service, password, psycopg |
| `ImportError: relative import` | L3 | use `python -m infra.fulfillment.cli`, not `python infra/fulfillment/cli.py` |
| `load-control-plane.ps1` blocked | L0 | `Set-ExecutionPolicy -Scope Process Bypass` then dot-source |
| `set KALLON_ENROLLMENT_URL` | L3 | set env or `--enrollment-url` before fulfill-order |
| Hub provision SSH fail | L1 | `KALLON_OPS_SSH_IDENTITY_FILE`, PEM ACLs |
| `401 invalid token` on enroll | L3/L4 | token in `device.env` vs registry hash |
| LE / HTTPS timeout | L2 | DNS IP, Starlink port forward, or use tunnel/ngrok |
| nginx/Caddy conflict | L2 | only one listener on :80/:443; stop nginx |
| `308` on `127.0.0.1/healthz` | L2 | use Host header or HTTPS URL (Caddy redirect) |
| WG no handshake | L4 | API peer-add logs, hub UDP 51820 |
| RTSP fail over VPN | L4 | mediamtx, iptables module 90, peer /32 |

---

## Architecture at rest (target picture)

```text
                    ┌──────────────────────────────────────┐
                    │  CONTROL PLANE (Artemis)              │
                    │  ┌─────────┐    ┌─────────────────┐  │
                    │  │ Postgres│◄───│ Enrollment API  │  │
                    │  │ :5432   │    │ :8000 loopback  │  │
                    │  └────▲────┘    └────────┬────────┘  │
                    │       │ fulfill-order    │ SSH       │
                    │  ┌────┴────┐             │           │
                    │  │ Ops CLI │             │           │
                    │  └─────────┘             │           │
                    │  ┌──────────────────────┴─────────┐  │
                    │  │ Caddy / Cloudflare / ngrok     │  │
                    │  └──────────────┬─────────────────┘  │
                    └─────────────────┼────────────────────┘
                                      │ HTTPS
         ┌────────────────────────────┼────────────────────────────┐
         │                            │                            │
    ┌────▼─────┐               ┌──────▼──────┐              ┌──────▼──────┐
    │ Jetson   │  WG UDP       │  Hub VPS    │              │  Buyer      │
    │ tower    │◄────────────►│  per cust   │              │  dashboard  │
    │ RTSP     │               │  alerts     │──────────────►│  (future) │
    └──────────┘               └─────────────┘   RTSP/HMAC   └─────────────┘
```

---

*Terra Industries · June 2026*
