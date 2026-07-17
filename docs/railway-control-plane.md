# Railway control plane — enrollment + Platform API

**Branch:** `railway`  
**Goal:** Replace Artemis (Windows + NSSM + ngrok) with a managed HTTPS control
plane on [Railway](https://railway.app), keeping Lightsail hubs unchanged.

Related: `docs/architecture-setup-guide.md` Option D, `docs/identity-and-secrets.md`.

---

## What ships in this branch

| File | Role |
|------|------|
| [`Dockerfile`](../Dockerfile) | Python 3.12 image + OpenSSH client + enrollment-api + registry + add-peer script |
| [`railway.toml`](../railway.toml) | Dockerfile build, `/healthz` check, `init-schema` release, **1 replica** |
| [`infra/enrollment-api/deploy/railway/entrypoint.sh`](../infra/enrollment-api/deploy/railway/entrypoint.sh) | Materialize ops PEM from env, bind `0.0.0.0:$PORT` |
| [`infra/enrollment-api/deploy/railway/env.example`](../infra/enrollment-api/deploy/railway/env.example) | Required Railway variables |

Hub provisioning (`infra/hub-provisioner`) stays on a laptop / Artemis — only the
**HTTP control plane** moves to Railway.

---

## Constraints (read before cutover)

1. **Single replica** — confirm tokens and alert SSE are in-memory (`railway.toml`
   sets `numReplicas = 1`). Do not scale out until those are externalized.
2. **Outbound SSH to hubs** — enrollment peer-add SSHes to
   `customers.gateway_endpoint` host as `ubuntu` with `terra-hub-ops.pem`.
   Lightsail firewall / hub `sshd` must allow Railway egress on **TCP 22**.
3. **Tower URL** — Jetsons bake `ENROLLMENT_URL`. Update factory + lab
   `device.env` (or put a stable DNS CNAME in front of Railway).

---

## Deploy (first time)

### 1. Railway project

1. Create a Railway project from this GitHub repo (`Yaqcodes/kallon-sentry`), branch `railway`.
2. Root directory: **repo root** (Dockerfile lives there).
3. Add **Postgres** plugin to the same project (Railway injects `DATABASE_URL`).

### 2. Variables

Copy from [`env.example`](../infra/enrollment-api/deploy/railway/env.example).

Minimum:

```text
KALLON_REGISTRY=postgres
KALLON_PEER_BACKEND=subprocess
KALLON_PROXY_VIA_HUB=1
KALLON_HUB_PROXY_TOKEN=<same as hub HUB_PROXY_TOKEN>
KALLON_PLATFORM_API_KEY=<dashboard key>
KALLON_CORS_ORIGINS=https://<your-vercel-app>,http://localhost:5173
KALLON_OPS_SSH_IDENTITY_B64=<base64 of terra-hub-ops.pem>
```

Generate base64 on Windows:

```powershell
[Convert]::ToBase64String(
  [IO.File]::ReadAllBytes('C:\kallon\secrets\terra-hub-ops.pem')
)
```

### 3. Generate domain

Railway → Settings → Networking → **Generate domain**.  
Note the HTTPS base, e.g. `https://kallon-api-production.up.railway.app`.

### 4. Allow hub SSH from Railway

On each hub (or Lightsail networking), ensure TCP **22** accepts Railway’s
egress. Options:

- Temporarily allow `0.0.0.0/0` on 22 (key-only auth; ops PEM still required), or
- Pin Railway static egress / allowlist once you know outbound IPs.

Smoke from a one-off Railway shell (or after deploy logs):

```bash
ssh -i /etc/kallon/terra-hub-ops.pem -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  ubuntu@<hub-public-ip> 'echo ok'
```

### 5. Smoke tests

```powershell
$base = "https://<railway-domain>"
curl.exe -sS "$base/healthz"

# Platform (needs API key)
curl.exe -sS -H "X-Kallon-Api-Key: $env:KALLON_PLATFORM_API_KEY" `
  "$base/v1/customers/cust_lab"

curl.exe -sS -H "X-Kallon-Api-Key: $env:KALLON_PLATFORM_API_KEY" `
  "$base/v1/towers/kln_lab_000001/live/cam1/index.m3u8"
```

Enroll a lab tower (or re-run enroll) and confirm on the hub:

```bash
sudo wg show wg0
```

### 6. Cut towers + dashboard over

1. Set factory / Jetson `ENROLLMENT_URL=https://<railway-domain>/v1` (or `/v1/enroll` path as your enroll script expects — match Artemis).
2. Dashboard login / `VITE_PLATFORM_URL` → Railway HTTPS base (no ngrok).
3. Update registry hub endpoint if needed (`python -m registry.cli set-hub …`) using `DATABASE_URL` from Railway (ops laptop).
4. Retire Artemis NSSM + ngrok when smoke stays green.

### 7. Migrate existing Postgres data (optional)

If Artemis already has customers/towers:

```powershell
# On Artemis
pg_dump -Fc kallon > kallon.dump

# Restore into Railway Postgres (use public DATABASE_URL + SSL)
pg_restore --clean --if-exists -d "$env:DATABASE_URL" kallon.dump
```

Then skip relying on empty `init-schema` alone (releaseCommand still harmless).

---

## Local Docker smoke

```powershell
cd <repo-root>
docker build -t kallon-api .
docker run --rm -p 8000:8000 `
  -e DATABASE_URL=postgresql://... `
  -e KALLON_HUB_PROXY_TOKEN=... `
  -e KALLON_PLATFORM_API_KEY=... `
  -e KALLON_OPS_SSH_IDENTITY_B64=... `
  -e KALLON_INIT_SCHEMA=1 `
  kallon-api

curl.exe http://127.0.0.1:8000/healthz
```

---

## After cutover checklist

- [ ] `/healthz` 200 on Railway domain  
- [ ] Dashboard CORS works (no ngrok interstitial)  
- [ ] `set-hub` / customers show correct Lightsail IP  
- [ ] Live HLS playlist 200 for lab tower  
- [ ] New enroll adds WG peer on hub  
- [ ] Artemis ngrok stopped  
- [ ] Replicas still = 1  
