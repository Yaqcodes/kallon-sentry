# Kallon Integration Contract — RTSP + Signed Alert Webhook (v1)

**Terra Industries · Internal Engineering**

This is the **stable contract** the Terra dashboard (a separate workstream)
consumes. v1 exposes exactly two surfaces; nothing else is guaranteed:

1. **Live video** — RTSP over the customer WireGuard VPN.
2. **Events** — HMAC-signed JSON webhook delivered to the customer hub.

> Out of scope for v1: historical playback / DVR / archive. Live + events only.

---

## 1. RTSP (live video)

Each tower rebroadcasts its cameras with `mediamtx`, reachable **only** over the
customer VPN (the tower firewall drops `:8554` on every interface except `lo`
and `wg0`).

```
rtsp://<tower-vpn-ip>:8554/cam<n>
```

| Token | Meaning | Source |
|-------|---------|--------|
| `<tower-vpn-ip>` | the tower's `/32` in the customer subnet | `towers.vpn_ip` (registry) |
| `<n>` | 1-based camera index | order of `CAMERA_IPS` in `device.env` |

- Transport: **RTSP over TCP** (`-rtsp_transport tcp`).
- Example: `rtsp://10.50.0.2:8554/cam1`, `rtsp://10.50.0.2:8554/cam2`.
- The dashboard/relay must be a WireGuard peer in the customer subnet (the
  `x.x.x.10` NOC/ops address is reserved for this).
- Verify with: `ffprobe -rtsp_transport tcp rtsp://10.50.0.2:8554/cam1`.

---

## 2. Alert webhook (events)

The tower watchdog POSTs signed JSON to the customer hub alert listener over the
VPN. The hub verifies the HMAC and forwards verified events to the dashboard
ingest (`ALERT_FORWARD_URL`).

```
POST  http://<hub-vpn-ip>:8080/alerts
Content-Type: application/json
X-Kallon-Signature: sha256=<hex>
```

- `<hub-vpn-ip>` is the customer gateway IP (`x.x.x.1`), from
  `customers.hub_alert_url`.
- The hub listener also serves `GET /healthz`.
- The hub firewall allows `:8080/tcp` **only** from the customer VPN subnet.

### 2.1 Alert JSON schema

```json
{
  "device_id": "kln_acme_000042",
  "type": "tamper_impact",
  "severity": "critical",
  "ts": 1717600000,
  "detail": {
    "axis_mg": 312,
    "threshold_mg": 150
  }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `device_id` | string | `kln_<slug>_<serial>` |
| `type` | string | `tamper_impact`, `enclosure_open`, `stream_fail`, `temp_high`, `temp_clear`, `light_change`, … |
| `severity` | string | `info` \| `warning` \| `critical` |
| `ts` | integer | Unix seconds (UTC) |
| `detail` | object | type-specific payload (optional) |

> The canonical body is **compact JSON with sorted keys** (`json.dumps(obj,
> sort_keys=True, separators=(",", ":"))`). The signature is computed over those
> exact bytes — re-serialize identically before verifying.

### 2.2 Signature

`X-Kallon-Signature: sha256=<hex>` where:

```
hex = HMAC_SHA256(key = alert.key bytes, message = raw request body)
```

- `alert.key` is the 32-byte shared secret present on **both** the tower
  (`/etc/kallon/alert.key`) and the hub. See `docs/identity-and-secrets.md`.
- Compare in constant time. Reject on mismatch with HTTP 401.

### 2.3 Verification sample (Python)

```python
import hashlib
import hmac


def verify(body: bytes, signature_header: str, alert_key: bytes) -> bool:
    expected = hmac.new(alert_key, body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, provided)


# In a handler:
#   raw = await request.body()
#   if not verify(raw, request.headers["X-Kallon-Signature"], ALERT_KEY):
#       return 401
```

This is exactly what `infra/hub/alert_listener.py` (`verify()`) implements and
what `kallon_watchdog.py` (`_sign()`) produces. The contract is regression-
tested in `tests/test_alert_hmac.py`.

### 2.4 Delivery semantics

- **Dedup:** the watchdog suppresses repeat alerts of the same `type` within a
  60 s window.
- **Retries:** 3 attempts with backoff on transport failure.
- **At-least-once:** consumers must treat `(device_id, type, ts)` as an
  idempotency key.

---

## 3. What the dashboard team must provide

- A WireGuard peer in each customer subnet (for RTSP pull), or consume from the
  hub RTSP relay.
- An ingest endpoint set as `ALERT_FORWARD_URL` on each hub listener (verified
  alerts are POSTed through verbatim).

*Contract owner: Terra platform. Changes here are breaking — version this doc.*
