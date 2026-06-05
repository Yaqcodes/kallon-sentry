#!/usr/bin/env bash
# 80-watchdogs.sh — install kallon-watchdog (+ optional PTZ daemon) as systemd.
#
# Renders units that run from /opt/kallon (populated by 70-app.sh) and read
# /etc/kallon/device.env. Ensures the alert HMAC key exists. The wg handshake
# watchdog timer is handled by 40-wireguard.sh.
#
# Idempotent.
source "$(dirname "$0")/lib.sh"

APP_DIR=/opt/kallon
KEY_FILE="$KALLON_CONFIG_DIR/alert.key"

ensure_alert_key() {
  if [[ -f "$KEY_FILE" ]]; then
    log "alert.key present"
    return
  fi
  head -c 32 /dev/urandom | base64 > "$KEY_FILE"
  chown root:khalifa "$KEY_FILE"
  chmod 0640 "$KEY_FILE"
  ok "generated $KEY_FILE (must match the hub verifier)"
}

write_watchdog_unit() {
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Rendered by scripts/install/80-watchdogs.sh — do not hand-edit.
[Unit]
Description=Kallon health and tamper watchdog
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
User=khalifa
Group=khalifa
SupplementaryGroups=gpio i2c
WorkingDirectory=$APP_DIR
EnvironmentFile=$KALLON_ENV
Environment=JETSON_MODEL_NAME=JETSON_ORIN_NANO
ExecStart=/usr/bin/python3 $APP_DIR/kallon_watchdog.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadOnlyPaths=$KALLON_CONFIG_DIR

[Install]
WantedBy=multi-user.target
EOF
  install -m 0644 -o root -g root "$tmp" /etc/systemd/system/kallon-watchdog.service
  rm -f "$tmp"
  ok "rendered kallon-watchdog.service"
}

write_ptz_unit() {
  default_var CAMERA_RTSP_USER admin
  local first_cam; first_cam="$(printf '%s' "${CAMERA_IPS:-}" | cut -d',' -f1 | tr -d ' ')"
  [[ -n "$first_cam" ]] || { warn "no camera IP; skipping PTZ daemon."; return; }

  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Rendered by scripts/install/80-watchdogs.sh — do not hand-edit.
[Unit]
Description=Kallon ONVIF PTZ daemon (JSON/TCP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=khalifa
Group=khalifa
WorkingDirectory=$APP_DIR
EnvironmentFile=$KALLON_ENV
Environment=PTZ_LISTEN_HOST=127.0.0.1
Environment=PTZ_LISTEN_PORT=8765
ExecStart=/usr/bin/python3 $APP_DIR/kallon_ptz_daemon.py \\
    --host $first_cam -P 80 -u $CAMERA_RTSP_USER \\
    --listen-host 127.0.0.1 --listen-port 8765
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  install -m 0644 -o root -g root "$tmp" /etc/systemd/system/kallon-ptz-daemon.service
  rm -f "$tmp"
  ok "rendered kallon-ptz-daemon.service (camera $first_cam)"
}

main() {
  require_root
  load_env
  ensure_alert_key
  write_watchdog_unit
  write_ptz_unit
  systemctl daemon-reload
  systemctl enable --now kallon-watchdog.service >/dev/null 2>&1 || warn "kallon-watchdog did not start."
  if [[ -f /etc/systemd/system/kallon-ptz-daemon.service ]]; then
    systemctl enable --now kallon-ptz-daemon.service >/dev/null 2>&1 || warn "kallon-ptz-daemon did not start."
  fi
  ok "watchdog module complete"
}

main "$@"
