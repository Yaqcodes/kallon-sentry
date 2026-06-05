#!/usr/bin/env bash
# kallon-wg-provision.sh — generate the Jetson WG keypair and render wg0.conf.
#
# Keys are generated ONCE and never rotated unless --regenerate-keys is passed.
# The private key never leaves the device; the public key is printed to stdout
# (and written to .public) for registration with the hub / registry.
#
# Usage:
#   sudo scripts/kallon-wg-provision.sh [--env FILE] [--regenerate-keys]
#                                       [--print-pubkey]
#
# Reads from device.env: WG_PRIVATE_KEY_PATH, VPN_IP, GATEWAY_PUBLIC_KEY,
# GATEWAY_ENDPOINT, VPN_SUBNET.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="/etc/kallon/device.env"
REGEN=0
PRINT_ONLY=0
WG_CONF=/etc/wireguard/wg0.conf

log() { printf '\033[0;36m%s\033[0m\n' "$*"; }
ok()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)             ENV_FILE="$2"; shift 2 ;;
    --regenerate-keys) REGEN=1; shift ;;
    --print-pubkey)    PRINT_ONLY=1; shift ;;
    -h|--help)         sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (sudo)."
command -v wg >/dev/null 2>&1 || die "wg not found (run 10-packages.sh first)."
[[ -f "$ENV_FILE" ]] || die "env not found: $ENV_FILE"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

WG_PRIVATE_KEY_PATH="${WG_PRIVATE_KEY_PATH:-/etc/wireguard/jetson.private}"
PUB_PATH="${WG_PRIVATE_KEY_PATH%.private}.public"

install -d -m 0700 -o root -g root "$(dirname "$WG_PRIVATE_KEY_PATH")"

# ── keys ─────────────────────────────────────────────────────────────────────
if [[ -f "$WG_PRIVATE_KEY_PATH" && $REGEN -eq 0 ]]; then
  log "keypair exists; keeping (use --regenerate-keys to rotate)."
else
  [[ $REGEN -eq 1 ]] && log "rotating keypair (--regenerate-keys)."
  umask 077
  wg genkey | tee "$WG_PRIVATE_KEY_PATH" | wg pubkey > "$PUB_PATH"
  chmod 600 "$WG_PRIVATE_KEY_PATH"; chmod 644 "$PUB_PATH"
  ok "generated keypair at $WG_PRIVATE_KEY_PATH"
fi

PRIV="$(cat "$WG_PRIVATE_KEY_PATH")"
PUB="$(cat "$PUB_PATH")"

if [[ $PRINT_ONLY -eq 1 ]]; then
  echo "$PUB"
  exit 0
fi

# ── render wg0.conf ──────────────────────────────────────────────────────────
: "${VPN_IP:?VPN_IP unset}"; : "${GATEWAY_PUBLIC_KEY:?}"; : "${GATEWAY_ENDPOINT:?}"; : "${VPN_SUBNET:?}"

tmp="$(mktemp)"
sed -e "s#__JETSON_PRIVATE_KEY__#${PRIV}#" \
    -e "s#__GATEWAY_PUBLIC_KEY__#${GATEWAY_PUBLIC_KEY}#" \
    -e "s#10.50.0.2/32#${VPN_IP}#" \
    -e "s#203.0.113.42:51820#${GATEWAY_ENDPOINT}#" \
    -e "s#10.50.0.0/24#${VPN_SUBNET}#" \
    "$REPO_DIR/deploy/wg0.conf.example" \
  | grep -v '^#' | grep -v '^[[:space:]]*$' > "$tmp"

if [[ -f "$WG_CONF" ]] && cmp -s "$tmp" "$WG_CONF"; then
  log "wg0.conf unchanged"
  rm -f "$tmp"
else
  install -m 0600 -o root -g root "$tmp" "$WG_CONF"
  rm -f "$tmp"
  ok "rendered $WG_CONF"
fi

echo
ok "Jetson WireGuard public key (register with hub/registry):"
echo "$PUB"
