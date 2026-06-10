# Order fulfillment — internal automation

**Terra Industries · Internal Engineering**

One command per customer order on the control plane. Buyers never run this;
factory/ops runs it when hardware is being prepared.

| Related | Role |
|---------|------|
| `docs/postgres-windows-server-setup.md` | One-time platform bring-up (Postgres, API service, TLS) |
| `docs/field-test-setup.md` | End-to-end validation |
| `infra/fulfillment/cli.py` | Implementation |

---

## What it does

`kallon-fulfill-order` chains:

1. **Customer** — create `cust_<slug>` if missing; auto-assign next `/24` (`10.50`, `10.51`, …)
2. **Hub** — run `kallon-hub-provision` if hub is not `active` (Lightsail creates VPS; `manual` uses `--host`)
3. **Towers** — `register-tower` × N; render `device_<device_id>.env` per unit (`CAMERA_IPS`, tokens, `ENROLLMENT_URL`)
4. **Manifest** — `fulfillment_cust_<slug>.json` (tokens — treat as secret)

After ship, towers **auto-enroll** on first boot (`kallon-enroll.service`) — no further ops steps.

---

## Prerequisites

- Path P control plane up: Postgres, enrollment API **service**, `https://enroll.<domain>/v1`
- `KALLON_ENROLLMENT_URL` set (or pass `--enrollment-url`)
- For hub SSH: `KALLON_OPS_SSH_IDENTITY_FILE`, `KALLON_OPS_SSH_PUBKEY_FILE`

Load env once per session:

```powershell
. .\scripts\load-control-plane.ps1
$env:KALLON_ENROLLMENT_URL = "https://enroll.yourdomain.com/v1"
```

---

## Examples

**Dry-run (plan only):**

```powershell
python infra/fulfillment/cli.py acme --display-name "Acme Security" `
  --towers 3 --cameras 2 --provider lightsail --region us-east-2 --dry-run
```

**First hub (existing Lightsail — lab customer):**

```powershell
python infra/fulfillment/cli.py lab --display-name "Kallon Lab" `
  --provider manual --host 18.220.75.237 `
  --towers 1 --cameras 2 `
  --subnet 10.50.0.0/24 `
  --output-dir C:\kallon\factory\lab
```

**New retail customer (new Lightsail hub + 3 towers, 2 cameras each):**

```powershell
python infra/fulfillment/cli.py acme --display-name "Acme Security" `
  --provider lightsail --region us-east-2 `
  --towers 3 --cameras 2 `
  --output-dir C:\kallon\factory\acme
```

---

## Camera count

Not stored in Postgres. Each `device_*.env` gets:

```text
CAMERA_IPS=192.168.10.108,192.168.10.109   # --cameras 2
RTSP_URLS=rtsp://127.0.0.1:8554/cam1,rtsp://127.0.0.1:8554/cam2
```

`kallon-jetson-install.sh` renders mediamtx from `CAMERA_IPS`. VPN fields are filled at enroll.

---

## QR / claim code

Each tower gets a `claim_code` (`clm_…`) in registry + `device.env`. QR label encodes it for dashboard linking (`qr_payload` in manifest). Enrollment uses `device_id` + `enrollment_token` from `device.env`.

---

## Subnet plan

| Customer | Subnet (auto) |
|----------|----------------|
| 1st new | `10.50.0.0/24` |
| 2nd | `10.51.0.0/24` |
| … | increment second octet |

Override with `--subnet` for the first hub (`cust_lab` on existing `10.50.0.0/24`).

Tower VPN IPs inside each `/24` are allocated automatically at enroll (`.2`, `.3`, …).

---

## Factory steps after fulfill-order

1. Copy each `device_<id>.env` → Jetson `/etc/kallon/device.env`
2. Sync hub `alert.key` → tower `/etc/kallon/alert.key`
3. `sudo scripts/kallon-jetson-install.sh --env /etc/kallon/device.env`
4. Enable `kallon-enroll.service`
5. `kallon-acceptance.sh` → ship

---

*Terra Industries · June 2026*
