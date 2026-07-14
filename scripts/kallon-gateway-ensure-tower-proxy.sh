#!/usr/bin/env bash
# kallon-gateway-ensure-tower-proxy.sh — install/update hub tower HTTP proxy.
#
# For existing hubs (idempotent). Fresh hubs get the same unit from
# kallon-gateway-init.sh. Run on the hub as root:
#
#   sudo HUB_PROXY_TOKEN=secret bash scripts/kallon-gateway-ensure-tower-proxy.sh
#   # or: sudo --preserve-env=HUB_PROXY_TOKEN bash ...
#
# Env:
#   HUB_PROXY_TOKEN   required (written into systemd Environment=)
#   HUB_PROXY_PORT    default 8767
#   HUB_PROXY_BIND    default 0.0.0.0
#   TOWER_PROXY_FILE  optional path to tower_proxy.py on the invoking machine
set -euo pipefail

log() { printf '[gw-tower-proxy] %s\n' "$*"; }
die() { printf '[gw-tower-proxy] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root."

KALLON_DIR=/opt/kallon-hub
HUB_PROXY_PORT="${HUB_PROXY_PORT:-8767}"
HUB_PROXY_BIND="${HUB_PROXY_BIND:-0.0.0.0}"
HUB_PROXY_TOKEN="${HUB_PROXY_TOKEN:-}"
[[ -n "$HUB_PROXY_TOKEN" ]] || die "set HUB_PROXY_TOKEN (shared with Artemis KALLON_HUB_PROXY_TOKEN)"

install -d -m 0755 "$KALLON_DIR"

_src=""
if [[ -n "${TOWER_PROXY_FILE:-}" && -f "$TOWER_PROXY_FILE" ]]; then
  _src="$TOWER_PROXY_FILE"
else
  for _c in \
    "$(dirname "$0")/../infra/hub/tower_proxy.py" \
    "$(dirname "$0")/infra/hub/tower_proxy.py" \
    "$KALLON_DIR/tower_proxy.py"; do
    [[ -f "$_c" ]] && { _src="$_c"; break; }
  done
fi
[[ -n "$_src" ]] || die "tower_proxy.py not found (pass TOWER_PROXY_FILE=...)"

install -m 0755 "$_src" "$KALLON_DIR/tower_proxy.py"
log "installed $KALLON_DIR/tower_proxy.py"

# Persist token for restarts (mode 0640).
install -d -m 0750 /etc/kallon
umask 027
printf 'HUB_PROXY_TOKEN=%s\n' "$HUB_PROXY_TOKEN" > /etc/kallon/hub-proxy.env
chmod 0640 /etc/kallon/hub-proxy.env

cat > /etc/systemd/system/kallon-tower-proxy.service <<EOF
[Unit]
Description=Kallon hub tower HTTP proxy (Artemis → wg0 → tower gateway)
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/kallon/hub-proxy.env
Environment=HUB_PROXY_BIND=${HUB_PROXY_BIND}
Environment=HUB_PROXY_PORT=${HUB_PROXY_PORT}
Environment=TOWER_GATEWAY_PORT=8766
ExecStart=/usr/bin/python3 ${KALLON_DIR}/tower_proxy.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now kallon-tower-proxy.service

if command -v ufw >/dev/null 2>&1; then
  ufw allow "${HUB_PROXY_PORT}/tcp" >/dev/null 2>&1 || true
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw reload >/dev/null 2>&1 || true
  fi
  log "ufw: allow ${HUB_PROXY_PORT}/tcp"
fi

log "OK: kallon-tower-proxy listening on ${HUB_PROXY_BIND}:${HUB_PROXY_PORT}"
log "healthz: curl -s http://127.0.0.1:${HUB_PROXY_PORT}/healthz"
