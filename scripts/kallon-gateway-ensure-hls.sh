#!/usr/bin/env bash
# kallon-gateway-ensure-hls.sh — install/update hub HLS remux + agent.
#
# For existing hubs (idempotent). Fresh hubs get the same stack from
# kallon-gateway-init.sh. Run on the hub as root:
#
#   sudo HUB_PROXY_TOKEN=secret bash scripts/kallon-gateway-ensure-hls.sh
#
# Env:
#   HUB_PROXY_TOKEN   required (shared with Artemis KALLON_HUB_PROXY_TOKEN)
#   HUB_HLS_PORT      default 8768
#   HUB_HLS_BIND      default 0.0.0.0
#   HLS_PROXY_FILE    optional path to hls_proxy.py
#   MEDIAMTX_YML_SRC  optional path to mediamtx-hub.yml
set -euo pipefail

log() { printf '[gw-hls] %s\n' "$*"; }
die() { printf '[gw-hls] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root."

KALLON_DIR=/opt/kallon-hub
HUB_HLS_PORT="${HUB_HLS_PORT:-8768}"
HUB_HLS_BIND="${HUB_HLS_BIND:-0.0.0.0}"
HUB_PROXY_TOKEN="${HUB_PROXY_TOKEN:-}"
[[ -n "$HUB_PROXY_TOKEN" ]] || die "set HUB_PROXY_TOKEN (shared with Artemis KALLON_HUB_PROXY_TOKEN)"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export KALLON_DIR

# MediaMTX first.
if [[ -f "$SCRIPT_DIR/kallon-hub-install-mediamtx.sh" ]]; then
  bash "$SCRIPT_DIR/kallon-hub-install-mediamtx.sh"
elif [[ -f /tmp/kallon-hub-install-mediamtx.sh ]]; then
  bash /tmp/kallon-hub-install-mediamtx.sh
else
  die "kallon-hub-install-mediamtx.sh not found"
fi

install -d -m 0755 "$KALLON_DIR"

_src=""
if [[ -n "${HLS_PROXY_FILE:-}" && -f "$HLS_PROXY_FILE" ]]; then
  _src="$HLS_PROXY_FILE"
else
  for _c in \
    "$SCRIPT_DIR/../infra/hub/hls_proxy.py" \
    "$SCRIPT_DIR/infra/hub/hls_proxy.py" \
    "$KALLON_DIR/hls_proxy.py" \
    /tmp/infra/hub/hls_proxy.py; do
    [[ -f "$_c" ]] && { _src="$_c"; break; }
  done
fi
[[ -n "$_src" ]] || die "hls_proxy.py not found (pass HLS_PROXY_FILE=...)"

install -m 0755 "$_src" "$KALLON_DIR/hls_proxy.py"
log "installed $KALLON_DIR/hls_proxy.py"

# Reuse /etc/kallon/hub-proxy.env token (same as tower-proxy).
install -d -m 0750 /etc/kallon
if [[ -f /etc/kallon/hub-proxy.env ]]; then
  # Ensure token line matches (overwrite token; keep file simple).
  umask 027
  printf 'HUB_PROXY_TOKEN=%s\n' "$HUB_PROXY_TOKEN" > /etc/kallon/hub-proxy.env
  chmod 0640 /etc/kallon/hub-proxy.env
else
  umask 027
  printf 'HUB_PROXY_TOKEN=%s\n' "$HUB_PROXY_TOKEN" > /etc/kallon/hub-proxy.env
  chmod 0640 /etc/kallon/hub-proxy.env
fi

cat > /etc/systemd/system/kallon-hls-proxy.service <<EOF
[Unit]
Description=Kallon hub HLS proxy (Artemis → MediaMTX ← tower RTSP over wg0)
After=network-online.target kallon-hub-mediamtx.service wg-quick@wg0.service
Wants=network-online.target kallon-hub-mediamtx.service

[Service]
Type=simple
EnvironmentFile=/etc/kallon/hub-proxy.env
Environment=HUB_HLS_BIND=${HUB_HLS_BIND}
Environment=HUB_HLS_PORT=${HUB_HLS_PORT}
Environment=MEDIAMTX_API=http://127.0.0.1:9997
Environment=MEDIAMTX_HLS=http://127.0.0.1:8888
Environment=TOWER_RTSP_PORT=8554
ExecStart=/usr/bin/python3 ${KALLON_DIR}/hls_proxy.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now kallon-hls-proxy.service

if command -v ufw >/dev/null 2>&1; then
  ufw allow "${HUB_HLS_PORT}/tcp" >/dev/null 2>&1 || true
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw reload >/dev/null 2>&1 || true
  fi
  log "ufw: allow ${HUB_HLS_PORT}/tcp"
fi

log "OK: kallon-hls-proxy on ${HUB_HLS_BIND}:${HUB_HLS_PORT}"
log "healthz: curl -s http://127.0.0.1:${HUB_HLS_PORT}/healthz"
