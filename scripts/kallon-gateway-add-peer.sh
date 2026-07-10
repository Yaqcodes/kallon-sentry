#!/usr/bin/env bash
# kallon-gateway-add-peer.sh — add (or update) a tower as a WireGuard peer.
#
# Idempotent: persists the peer to wg0.conf AND applies it live with `wg set`.
# Called by the enrollment API on first-boot, or by hand for pre-provisioned
# towers. When --gateway-host is the local hub, runs locally; otherwise SSHes.
#
# Usage:
#   kallon-gateway-add-peer.sh --pubkey <b64> --vpn-ip 10.50.0.2/32 \
#       --device-id kln_acme_000042 [--gateway-host 203.0.113.42] [--ssh-user ubuntu]
set -euo pipefail

PUBKEY=""; VPN_IP=""; DEVICE_ID=""; GATEWAY_HOST=""; SSH_USER="ubuntu"; WG_IFACE="wg0"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pubkey)       PUBKEY="$2"; shift 2 ;;
    --vpn-ip)       VPN_IP="$2"; shift 2 ;;
    --device-id)    DEVICE_ID="$2"; shift 2 ;;
    --gateway-host) GATEWAY_HOST="$2"; shift 2 ;;
    --ssh-user)     SSH_USER="$2"; shift 2 ;;
    --iface)        WG_IFACE="$2"; shift 2 ;;
    *) die "unknown arg: $1" ;;
  esac
done

[[ -n "$PUBKEY" ]] || die "--pubkey required"
[[ -n "$VPN_IP" ]] || die "--vpn-ip required (e.g. 10.50.0.2/32)"
[[ "$VPN_IP" == */* ]] || VPN_IP="${VPN_IP}/32"

# The actual peer-add logic, executed on the hub host.
remote_script() {
cat <<'REMOTE'
set -euo pipefail
PUBKEY="$1"; VPN_IP="$2"; DEVICE_ID="$3"; WG_IFACE="$4"
CONF="/etc/wireguard/${WG_IFACE}.conf"
[ -f "$CONF" ] || { echo "ERROR: $CONF missing (run kallon-gateway-init.sh)"; exit 1; }

# Live apply (idempotent: wg set replaces allowed-ips for an existing peer).
wg set "$WG_IFACE" peer "$PUBKEY" allowed-ips "$VPN_IP"

# Persist to wg0.conf: remove any existing block for this pubkey, then append.
# This is the canonical algorithm mirrored in infra/hub/wg_peers.py (tested).
python3 - "$CONF" "$PUBKEY" "$VPN_IP" "$DEVICE_ID" <<'PY'
import sys, re
conf, pub, vpn, dev = sys.argv[1:5]
text = open(conf).read()
blocks = re.split(r'(?m)^\[Peer\]\s*$', text)
head = blocks[0]
# Drop existing block matching this key OR this device-id (key rotation safe).
kept = [b for b in blocks[1:]
        if f"PublicKey={pub}" not in b.replace(" ", "") and f"#{dev}" not in b.replace(" ", "")]
out = head.rstrip() + "\n"
for b in kept:
    out += "[Peer]" + b.rstrip() + "\n"
out += f"\n[Peer]\n# {dev}\nPublicKey = {pub}\nAllowedIPs = {vpn}\n"
open(conf, "w").write(out)
print(f"persisted peer {dev} ({pub[:12]}...) -> {vpn}")
PY
REMOTE
}

if [[ -z "$GATEWAY_HOST" || "$GATEWAY_HOST" == "localhost" || "$GATEWAY_HOST" == "127.0.0.1" ]]; then
  [[ ${EUID:-$(id -u)} -eq 0 ]] || die "local peer-add must run as root."
  bash -c "$(remote_script)" _ "$PUBKEY" "$VPN_IP" "$DEVICE_ID" "$WG_IFACE"
else
  SSH_IDENTITY=()
  if [[ -n "${KALLON_OPS_SSH_IDENTITY_FILE:-}" && -f "${KALLON_OPS_SSH_IDENTITY_FILE}" ]]; then
    SSH_IDENTITY=(-i "$KALLON_OPS_SSH_IDENTITY_FILE" -o IdentitiesOnly=yes -o BatchMode=yes)
  fi
  ssh "${SSH_IDENTITY[@]}" -o StrictHostKeyChecking=accept-new "${SSH_USER}@${GATEWAY_HOST}" \
    "sudo bash -s -- '$PUBKEY' '$VPN_IP' '$DEVICE_ID' '$WG_IFACE'" <<REMOTE
$(remote_script)
REMOTE
fi

echo "OK: peer ${DEVICE_ID:-$PUBKEY} -> ${VPN_IP} on ${GATEWAY_HOST:-local} ${WG_IFACE}"
