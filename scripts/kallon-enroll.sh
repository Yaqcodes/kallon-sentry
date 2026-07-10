#!/usr/bin/env bash
# kallon-enroll.sh — first-boot auto-enrollment against the Terra enrollment API.
#
# Flow:
#   1. Generate the WG keypair (no wg0.conf yet) and read the public key.
#   2. POST /v1/enroll with device_id/claim_code + token + wg_public_key.
#   3. Persist the returned hub config into device.env.
#   4. Render wg0.conf (kallon-wg-provision.sh) and bring up wg0.
#   5. On a live handshake, POST /v1/enroll/confirm.
#   6. Touch /etc/kallon/.enrolled so the one-shot service never re-runs.
#
# Idempotent + safe to retry. Guarded by /etc/kallon/.enrolled.
# Two layers of retry, so a single flaky run never strands a tower:
#   * within a run: MAX_TRIES POSTs to /v1/enroll with backoff (network blips)
#   * across runs: kallon-enroll.timer re-invokes this whole script every few
#     minutes until .enrolled exists (systemd-level, zero maintenance)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WG_PROVISION=(bash "$SCRIPT_DIR/kallon-wg-provision.sh")
ENV_FILE="/etc/kallon/device.env"
ENROLLED_MARKER="/etc/kallon/.enrolled"
MAX_TRIES="${MAX_TRIES:-30}"
SLEEP_BASE="${SLEEP_BASE:-5}"
# How long to wait for a live handshake after wg-quick comes up. The hub now
# adds the peer over SSH inside /v1/enroll (with its own retries), which can
# take longer than a bare local check — give it real margin before giving up.
# A failed run here is NOT the end of the world: kallon-enroll.timer retries
# the whole flow every few minutes until /etc/kallon/.enrolled exists.
HANDSHAKE_TRIES="${HANDSHAKE_TRIES:-24}"
HANDSHAKE_POLL_SEC="${HANDSHAKE_POLL_SEC:-5}"

log() { printf '\033[0;36m[enroll] %s\033[0m\n' "$*"; }
ok()  { printf '\033[0;32m[enroll] %s\033[0m\n' "$*"; }
die() { printf '\033[0;31m[enroll] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "${1:-}" == "--env" ]] && { ENV_FILE="$2"; shift 2; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root."
command -v curl >/dev/null || die "curl required"
command -v jq   >/dev/null || die "jq required"

if [[ -f "$ENROLLED_MARKER" ]]; then
  ok "already enrolled ($ENROLLED_MARKER present); nothing to do."
  exit 0
fi

[[ -f "$ENV_FILE" ]] || die "env not found: $ENV_FILE"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${ENROLLMENT_URL:?ENROLLMENT_URL unset}"
: "${ENROLLMENT_TOKEN:?ENROLLMENT_TOKEN unset}"
: "${DEVICE_ID:?}"

# ── 1. keypair + pubkey ───────────────────────────────────────────────────────
PUBKEY="$("${WG_PROVISION[@]}" --env "$ENV_FILE" --print-pubkey)"
[[ -n "$PUBKEY" ]] || die "failed to obtain WG public key"
log "device $DEVICE_ID pubkey ${PUBKEY:0:12}..."

# ── 2. enroll (retry/backoff) ─────────────────────────────────────────────────
req_body="$(jq -n \
  --arg d "$DEVICE_ID" --arg c "${CLAIM_CODE:-}" \
  --arg k "$PUBKEY" --arg t "$ENROLLMENT_TOKEN" \
  '{device_id:$d, wg_public_key:$k, enrollment_token:$t} + (if $c=="" then {} else {claim_code:$c} end)')"

resp=""
for try in $(seq 1 "$MAX_TRIES"); do
  log "enroll attempt $try/$MAX_TRIES → $ENROLLMENT_URL/enroll"
  if resp="$(curl -fsS --max-time 20 -X POST \
        -H 'Content-Type: application/json' \
        ${ENROLLMENT_HMAC_HEADER:+-H "$ENROLLMENT_HMAC_HEADER"} \
        --data "$req_body" "$ENROLLMENT_URL/enroll")"; then
    ok "enroll accepted"
    break
  fi
  resp=""
  sleep $(( SLEEP_BASE * try > 60 ? 60 : SLEEP_BASE * try ))
done
[[ -n "$resp" ]] || die "enrollment failed after $MAX_TRIES attempts"

VPN_IP="$(jq -r '.vpn_ip' <<<"$resp")"
VPN_SUBNET="$(jq -r '.vpn_subnet' <<<"$resp")"
GATEWAY_ENDPOINT="$(jq -r '.gateway_endpoint' <<<"$resp")"
GATEWAY_PUBLIC_KEY="$(jq -r '.gateway_public_key' <<<"$resp")"
ALERT_WEBHOOK_URL="$(jq -r '.alert_webhook_url' <<<"$resp")"
CONFIRM_TOKEN="$(jq -r '.confirm_token' <<<"$resp")"

# ── 3. persist hub config into device.env (idempotent upsert) ─────────────────
set_env() {  # set_env KEY VALUE
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s#^${key}=.*#${key}=${val}#" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}
set_env VPN_IP "$VPN_IP"
set_env VPN_SUBNET "$VPN_SUBNET"
set_env GATEWAY_ENDPOINT "$GATEWAY_ENDPOINT"
set_env GATEWAY_PUBLIC_KEY "$GATEWAY_PUBLIC_KEY"
set_env ALERT_WEBHOOK_URL "$ALERT_WEBHOOK_URL"
ok "wrote hub config to $ENV_FILE (vpn_ip=$VPN_IP)"

# ── 4. render wg0.conf + bring up ─────────────────────────────────────────────
"${WG_PROVISION[@]}" --env "$ENV_FILE" >/dev/null
systemctl restart wg-quick@wg0 || die "wg-quick@wg0 failed to start"

# ── 5. wait for handshake, then confirm ───────────────────────────────────────
hs_ok=false
for attempt in $(seq 1 "$HANDSHAKE_TRIES"); do
  hs="$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2; exit}')"
  now="$(date +%s)"
  if [[ -n "${hs:-}" && "$hs" != "0" && $((now - hs)) -lt 180 ]]; then hs_ok=true; break; fi
  log "waiting for handshake ($attempt/$HANDSHAKE_TRIES)..."
  sleep "$HANDSHAKE_POLL_SEC"
done

confirm_body="$(jq -n --arg d "$DEVICE_ID" --arg t "$CONFIRM_TOKEN" --argjson ok "$($hs_ok && echo true || echo false)" \
  '{device_id:$d, confirm_token:$t, handshake_ok:$ok}')"
if curl -fsS --max-time 20 -X POST -H 'Content-Type: application/json' \
     --data "$confirm_body" "$ENROLLMENT_URL/enroll/confirm" >/dev/null; then
  ok "enrollment confirmed (handshake_ok=$hs_ok)"
else
  log "confirm POST failed (will rely on hub handshake); continuing."
fi

$hs_ok || die "no WireGuard handshake yet; leaving unmarked for retry."

touch "$ENROLLED_MARKER"
ok "enrollment complete for $DEVICE_ID."
