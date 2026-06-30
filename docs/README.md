# Kallon documentation index

**Terra Industries · Internal Engineering · branch `field-test`**

**Repo layout:** [`README.md`](../README.md) · **`docs/`** (this folder) · **`planning/`** · **`legacy/`**

Start here for operations and setup. Strategy docs live in **`planning/`** at repo root. May 2025 bench material is in **`legacy/`**.

---

## What are you trying to do?

```text
Understand the whole system (architecture, components, diagnose)
  → project-official-reference.md

Stand up / operate production (Artemis + hub + tower)
  → architecture-setup-guide.md        (primary walkthrough)
  → postgres-windows-server-setup.md   (control plane detail)
  → field-test-setup.md                (validation + installer modules)

Fulfill a customer order (factory)
  → order-fulfillment.md               (CLI + business walkthrough)

Why we built it / product intent / phases
  → ../planning/sovereign-stack-brief.md
  → ../planning/mass-deployment-roadmap.md
  → ../planning/work-plan.md           (living task board)

Wire the dashboard (RTSP + alerts)
  → alert-webhook.md
  → identity-and-secrets.md

Hardware wiring on Jetson
  → hardware-wiring.md

ONVIF / PTZ dev CLI (optional)
  → dev-onvif-ptz.md
```

---

## Canonical doc set (`docs/`)

| Doc | Role |
|-----|------|
| **`project-official-reference.md`** | Single technical reference — architecture, diagnose |
| **`architecture-setup-guide.md`** | Layered setup: Artemis → hub → factory → enroll → live |
| **`postgres-windows-server-setup.md`** | Path P control plane (Postgres, API, TLS, hub §8) |
| **`field-test-setup.md`** | End-to-end validation, Path A/P, installer modules |
| **`order-fulfillment.md`** | `kallon-fulfill-order` + business walkthrough |
| **`alert-webhook.md`** | Dashboard integration contract (RTSP + HMAC) |
| **`identity-and-secrets.md`** | ID formats, secret locations |
| **`hardware-wiring.md`** | J12 pin map, sensor logic |
| **`dev-onvif-ptz.md`** | ONVIF / PTZ daemon CLI (developer bench) |

## Sibling folders (repo root)

| Folder | Role |
|--------|------|
| **`../planning/`** | Product brief, mass-deployment roadmap, living work plan |
| **`../legacy/`** | May 2025 bench archive — historical only |

## Redirect stubs in `docs/`

| Stub | Use instead |
|------|-------------|
| `customer-gateway.md` | `postgres-windows-server-setup.md` **§8** + `architecture-setup-guide.md` Phase 6 |
| `order-to-live-feed.md` | `order-fulfillment.md` **§ Business walkthrough** |

---

*Terra Industries · June 2026*
