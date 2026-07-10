#!/usr/bin/env bash
# kallon-gateway-init.sh — initialize a customer hub VM (any provider).
#
# Runs ON the hub host (Ubuntu). Idempotent. Sets up the WireGuard hub, UFW
# (incl. wg0 peer forwarding for NOC → tower RTSP), and the HMAC alert listener,
# then emits gateway_manifest.json on stdout.
#
# Peer forwarding is re-applied after wg0 is up via kallon-gateway-ensure-forwarding.sh.
# That ensure script also migrates hubs provisioned before the forwarding rule existed.
#
# Usage (run as root on the hub):
#   sudo kallon-gateway-init.sh \
#     --customer-id cust_acme \
#     --vpn-subnet 10.50.0.0/24 \
#     [--gateway-ip 10.50.0.1] [--listen-port 51820] \
#     [--public-endpoint 203.0.113.42]   # public IP/host for tower Endpoint
set -euo pipefail

CUSTOMER_ID=""; VPN_SUBNET=""; GATEWAY_IP=""; LISTEN_PORT=51820; PUBLIC_ENDPOINT=""
OPS_SSH_PUBKEY=""; OPS_SSH_PUBKEY_FILE=""; OPS_SSH_USER="ubuntu"
ALERT_LISTENER_FILE=""
ALERT_PORT=8080
KALLON_DIR=/opt/kallon-hub

log() { printf '\033[0;36m[gw-init] %s\033[0m\n' "$*" >&2; }
ok()  { printf '\033[0;32m[gw-init] %s\033[0m\n' "$*" >&2; }
die() { printf '\033[0;31m[gw-init] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --customer-id)     CUSTOMER_ID="$2"; shift 2 ;;
    --vpn-subnet)      VPN_SUBNET="$2"; shift 2 ;;
    --gateway-ip)      GATEWAY_IP="$2"; shift 2 ;;
    --listen-port)     LISTEN_PORT="$2"; shift 2 ;;
    --public-endpoint) PUBLIC_ENDPOINT="$2"; shift 2 ;;
    --alert-port)      ALERT_PORT="$2"; shift 2 ;;
    --ops-ssh-pubkey)      OPS_SSH_PUBKEY="$2"; shift 2 ;;
    --ops-ssh-pubkey-file) OPS_SSH_PUBKEY_FILE="$2"; shift 2 ;;
    --ops-ssh-user)        OPS_SSH_USER="$2"; shift 2 ;;
    --alert-listener-file) ALERT_LISTENER_FILE="$2"; shift 2 ;;
    *) die "unknown arg: $1" ;;
  esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root."
[[ -n "$CUSTOMER_ID" ]] || die "--customer-id required"
[[ -n "$VPN_SUBNET" ]]  || die "--vpn-subnet required"

# Default the gateway IP to the .1 of the subnet.
if [[ -z "$GATEWAY_IP" ]]; then
  GATEWAY_IP="$(python3 - "$VPN_SUBNET" <<'PY'
import ipaddress, sys
net = ipaddress.ip_network(sys.argv[1], strict=False)
print(str(ipaddress.ip_address(int(net.network_address) + 1)))
PY
)"
fi

# 1. packages ------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard-tools ufw python3 >/dev/null
ok "packages installed"

# 1b. Terra control-plane ops SSH key (one pubkey for all hubs; idempotent) ----
if [[ -n "$OPS_SSH_PUBKEY_FILE" && -f "$OPS_SSH_PUBKEY_FILE" ]]; then
  OPS_SSH_PUBKEY="$(head -1 "$OPS_SSH_PUBKEY_FILE")"
fi
if [[ -n "$OPS_SSH_PUBKEY" ]]; then
  homedir="$(getent passwd "$OPS_SSH_USER" | cut -d: -f6 || true)"
  [[ -n "$homedir" ]] || die "user $OPS_SSH_USER not found (cannot install ops SSH pubkey)"
  install -d -m 0700 "$homedir/.ssh"
  touch "$homedir/.ssh/authorized_keys"
  if ! grep -qF "$OPS_SSH_PUBKEY" "$homedir/.ssh/authorized_keys"; then
    echo "$OPS_SSH_PUBKEY" >> "$homedir/.ssh/authorized_keys"
  fi
  chown -R "$OPS_SSH_USER:$OPS_SSH_USER" "$homedir/.ssh"
  chmod 600 "$homedir/.ssh/authorized_keys"
  ok "Terra ops SSH pubkey installed for ${OPS_SSH_USER} (enrollment peer-add)"
fi

# 2. gateway keypair -----------------------------------------------------------
install -d -m 0700 /etc/wireguard
if [[ ! -f /etc/wireguard/gateway.private ]]; then
  umask 077
  wg genkey | tee /etc/wireguard/gateway.private | wg pubkey > /etc/wireguard/gateway.public
  chmod 600 /etc/wireguard/gateway.private; chmod 644 /etc/wireguard/gateway.public
  ok "generated gateway keypair"
fi
GW_PRIV="$(cat /etc/wireguard/gateway.private)"
GW_PUB="$(cat /etc/wireguard/gateway.public)"

# 3. wg0.conf [Interface] only — peers added later by kallon-gateway-add-peer.sh
if [[ ! -f /etc/wireguard/wg0.conf ]]; then
  cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = ${GATEWAY_IP}/24
ListenPort = ${LISTEN_PORT}
PrivateKey = ${GW_PRIV}
EOF
  chmod 600 /etc/wireguard/wg0.conf
  ok "wrote /etc/wireguard/wg0.conf"
else
  log "wg0.conf exists; leaving (peers managed by add-peer)."
fi

# 4. ip forwarding -------------------------------------------------------------
sysctl_file=/etc/sysctl.d/99-kallon-hub.conf
echo "net.ipv4.ip_forward = 1" > "$sysctl_file"
sysctl -p "$sysctl_file" >/dev/null
ok "ip_forward enabled"

# 5. UFW: WG open; alert port from VPN only; peer forwarding; deny the rest ---
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
ufw allow "${LISTEN_PORT}/udp" >/dev/null
ufw allow from "$VPN_SUBNET" to any port "$ALERT_PORT" proto tcp >/dev/null
# Hub-and-spoke: NOC/dashboard peers reach tower peers (RTSP :8554, SSH, etc.)
# over the VPN. ip_forward alone is not enough — UFW drops FORWARD by default.
ufw route allow in on wg0 out on wg0 >/dev/null
ufw --force enable >/dev/null
ok "ufw: ${LISTEN_PORT}/udp; ${ALERT_PORT}/tcp from ${VPN_SUBNET}; wg0 peer forwarding"

# 6. alert listener (systemd) --------------------------------------------------
install -d -m 0750 /etc/kallon
if [[ ! -f /etc/kallon/alert.key ]]; then
  head -c 32 /dev/urandom | base64 > /etc/kallon/alert.key
  chmod 0640 /etc/kallon/alert.key
  log "generated /etc/kallon/alert.key (copy this SAME value to each tower)."
fi
install -d -m 0755 "$KALLON_DIR"
_listener_src=""
if [[ -n "$ALERT_LISTENER_FILE" && -f "$ALERT_LISTENER_FILE" ]]; then
  _listener_src="$ALERT_LISTENER_FILE"
else
  for _c in "$(dirname "$0")/infra/hub/alert_listener.py" \
            "$(dirname "$0")/../infra/hub/alert_listener.py"; do
    [[ -f "$_c" ]] && { _listener_src="$_c"; break; }
  done
fi
if [[ -n "$_listener_src" ]]; then
  install -m 0755 "$_listener_src" "$KALLON_DIR/alert_listener.py"
fi
cat > /etc/systemd/system/kallon-alert-listener.service <<EOF
[Unit]
Description=Kallon hub alert listener (HMAC verifier)
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
Environment=ALERT_KEY_PATH=/etc/kallon/alert.key
Environment=ALERT_BIND=${GATEWAY_IP}
Environment=ALERT_PORT=${ALERT_PORT}
ExecStart=/usr/bin/python3 ${KALLON_DIR}/alert_listener.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# 7. bring up WG + listener ----------------------------------------------------
systemctl enable --now wg-quick@wg0 >/dev/null 2>&1 || die "wg-quick@wg0 failed"
if [[ -f "$KALLON_DIR/alert_listener.py" ]]; then
  systemctl enable --now kallon-alert-listener.service >/dev/null 2>&1 \
    || log "alert listener not started (will start once wg0 is up)."
fi

# 7b. Re-apply peer forwarding now wg0 exists (idempotent).
_ensure="${BASH_SOURCE%/*}/kallon-gateway-ensure-forwarding.sh"
if [[ -f "$_ensure" ]]; then
  bash "$_ensure"
else
  ufw route allow in on wg0 out on wg0 >/dev/null 2>&1 || true
  ufw reload >/dev/null 2>&1 || true
fi

# 8. emit manifest (stdout) ----------------------------------------------------
PUBLIC_ENDPOINT="${PUBLIC_ENDPOINT:-$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo CHANGE_ME)}"
cat <<EOF
{
  "customer_id": "${CUSTOMER_ID}",
  "gateway_endpoint": "${PUBLIC_ENDPOINT}:${LISTEN_PORT}",
  "gateway_public_key": "${GW_PUB}",
  "vpn_subnet": "${VPN_SUBNET}",
  "gateway_ip": "${GATEWAY_IP}",
  "alert_webhook_url": "http://${GATEWAY_IP}:${ALERT_PORT}/alerts"
}
EOF
ok "gateway init complete for ${CUSTOMER_ID}"
