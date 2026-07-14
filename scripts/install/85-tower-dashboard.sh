#!/usr/bin/env bash
# 85-tower-dashboard.sh — optional on-Jetson lab dashboard (loopback only).
#
# Installs when ENABLE_TOWER_DASHBOARD=1 in device.env:
#   - /opt/kallon/tower-dashboard/   (gateway + static SPA)
#   - /opt/kallon/alert_listener.py  (reused hub listener, loopback bind)
#   - kallon-tower-dashboard.service
#   - kallon-tower-alert-listener.service  (127.0.0.1:8080 → gateway ingest)
#   - optional Chromium kiosk autostart for the local monitor (login)
#   - optional Applications menu launcher (~/.local/share/applications/)
#
# When ENABLE_TOWER_DASHBOARD=0 (default), any dashboard units are stopped,
# disabled, and removed so fleet towers are unaffected.
#
# Idempotent.
source "$(dirname "$0")/lib.sh"

REPO_DIR="${REPO_DIR:-$(cd "$INSTALL_DIR/../.." && pwd)}"
APP_DIR=/opt/kallon
DASH_DIR="$APP_DIR/tower-dashboard"
LISTENER_SRC="$REPO_DIR/infra/hub/alert_listener.py"
GATEWAY_SRC="$REPO_DIR/infra/tower-dashboard/gateway.py"
MJPEG_SRC="$REPO_DIR/infra/tower-dashboard/mjpeg_proxy.py"
WEB_SRC="$REPO_DIR/infra/tower-dashboard/web"
MJPEG_PORT=8889

disable_dashboard() {
  for svc in kallon-tower-dashboard kallon-tower-alert-listener kallon-tower-mjpeg-proxy; do
    systemctl disable --now "${svc}.service" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${svc}.service"
  done
  rm -f "/home/$RUNTIME_USER/.config/autostart/kallon-tower-dashboard.desktop"
  rm -f "/home/$RUNTIME_USER/.local/share/applications/kallon-tower-dashboard.desktop"
  systemctl daemon-reload
}

find_browser() {
  for candidate in chromium-browser chromium google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

sync_dashboard_files() {
  [[ -f "$GATEWAY_SRC" ]] || die "missing $GATEWAY_SRC"
  [[ -d "$WEB_SRC" ]] || die "missing $WEB_SRC"
  [[ -f "$LISTENER_SRC" ]] || die "missing $LISTENER_SRC"

  ensure_dir "$DASH_DIR" 0755 root "$RUNTIME_USER"
  install_if_changed "$GATEWAY_SRC" "$DASH_DIR/gateway.py" 0644 root "$RUNTIME_USER" || true
  install_if_changed "$MJPEG_SRC"   "$DASH_DIR/mjpeg_proxy.py" 0644 root "$RUNTIME_USER" || true
  install_if_changed "$LISTENER_SRC" "$APP_DIR/alert_listener.py" 0644 root "$RUNTIME_USER" || true

  # Sync the static web tree (Vite build output from sentinel-console/).
  # Rebuild with: cd infra/tower-dashboard/sentinel-console && npm run build
  rm -rf "$DASH_DIR/web"
  cp -r "$WEB_SRC" "$DASH_DIR/web"
  chown -R root:"$RUNTIME_USER" "$DASH_DIR/web"
  find "$DASH_DIR/web" -type f -exec chmod 0644 {} +
  ok "synced tower-dashboard web/ → $DASH_DIR/web"
}

write_alert_listener_unit() {
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Rendered by scripts/install/85-tower-dashboard.sh — do not hand-edit.
[Unit]
Description=Kallon tower local alert listener (loopback HMAC sink)
After=network-online.target kallon-tower-dashboard.service
Wants=network-online.target

[Service]
Type=simple
User=${RUNTIME_USER}
Group=${RUNTIME_USER}
EnvironmentFile=$KALLON_ENV
Environment=ALERT_KEY_PATH=${ALERT_KEY_PATH:-/etc/kallon/alert.key}
Environment=ALERT_BIND=127.0.0.1
Environment=ALERT_PORT=8080
Environment=ALERT_FORWARD_URL=http://127.0.0.1:${TOWER_DASHBOARD_PORT}/ingest/alerts
ExecStart=/usr/bin/python3 $APP_DIR/alert_listener.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  install -m 0644 -o root -g root "$tmp" /etc/systemd/system/kallon-tower-alert-listener.service
  rm -f "$tmp"
  ok "rendered kallon-tower-alert-listener.service (127.0.0.1:8080 → dashboard ingest)"
}

write_mjpeg_proxy_unit() {
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Rendered by scripts/install/85-tower-dashboard.sh — do not hand-edit.
[Unit]
Description=Kallon tower MJPEG proxy (loopback, near-real-time kiosk video)
After=mediamtx.service
BindsTo=mediamtx.service

[Service]
Type=simple
User=${RUNTIME_USER}
Group=${RUNTIME_USER}
EnvironmentFile=$KALLON_ENV
Environment=MJPEG_BIND=127.0.0.1
Environment=MJPEG_PORT=${MJPEG_PORT}
Environment=MJPEG_DECODER=h264_nvv4l2dec
ExecStart=/usr/bin/python3 $DASH_DIR/mjpeg_proxy.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=kallon-tower-dashboard.service
EOF
  install -m 0644 -o root -g root "$tmp" /etc/systemd/system/kallon-tower-mjpeg-proxy.service
  rm -f "$tmp"
  ok "rendered kallon-tower-mjpeg-proxy.service (127.0.0.1:${MJPEG_PORT})"
}

write_dashboard_unit() {
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Rendered by scripts/install/85-tower-dashboard.sh — do not hand-edit.
[Unit]
Description=Sentinel tower dashboard gateway (loopback SPA + ingest)
After=network-online.target mediamtx.service kallon-watchdog.service kallon-ptz-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=${RUNTIME_USER}
Group=${RUNTIME_USER}
WorkingDirectory=$DASH_DIR
EnvironmentFile=$KALLON_ENV
Environment=DASH_BIND=wg0
Environment=DASH_PORT=${TOWER_DASHBOARD_PORT}
Environment=WEB_ROOT=$DASH_DIR/web
Environment=WATCHDOG_STATUS_URL=http://127.0.0.1:${TOWER_STATUS_API_PORT}
Environment=PTZ_HOST=127.0.0.1
Environment=PTZ_PORT=8765
Environment=MJPEG_PROXY=http://127.0.0.1:${MJPEG_PORT}
ExecStart=/usr/bin/python3 $DASH_DIR/gateway.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  install -m 0644 -o root -g root "$tmp" /etc/systemd/system/kallon-tower-dashboard.service
  rm -f "$tmp"
  ok "rendered kallon-tower-dashboard.service (wg0 + loopback :${TOWER_DASHBOARD_PORT})"
}

install_desktop_launcher() {
  local browser
  browser="$(find_browser)" || { warn "no Chromium/Chrome found; skipping desktop launcher."; return; }

  local apps="/home/$RUNTIME_USER/.local/share/applications"
  local icon_path="$DASH_DIR/web/app_icon.png"
  ensure_dir "$apps" 0755 "$RUNTIME_USER" "$RUNTIME_USER"
  local desktop="$apps/kallon-tower-dashboard.desktop"
  cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Sentinel
Comment=Local loopback console for bench tower
Exec=${browser} --app=http://127.0.0.1:${TOWER_DASHBOARD_PORT}/
Icon=${icon_path}
Terminal=false
Categories=Network;Monitor;System;
StartupNotify=true
EOF
  chown "$RUNTIME_USER:$RUNTIME_USER" "$desktop"
  chmod 0644 "$desktop"
  ok "installed Applications menu launcher ($browser → http://127.0.0.1:${TOWER_DASHBOARD_PORT}/)"
}

install_kiosk_autostart() {
  default_var TOWER_DASHBOARD_KIOSK 1
  [[ "${TOWER_DASHBOARD_KIOSK}" == "1" ]] || {
    rm -f "/home/$RUNTIME_USER/.config/autostart/kallon-tower-dashboard.desktop"
    log "kiosk autostart disabled (TOWER_DASHBOARD_KIOSK=0)"
    return
  }

  local browser
  browser="$(find_browser)" || { warn "no Chromium/Chrome found; skipping kiosk autostart."; return; }

  local autostart="/home/$RUNTIME_USER/.config/autostart"
  local icon_path="$DASH_DIR/web/app_icon.png"
  ensure_dir "$autostart" 0755 "$RUNTIME_USER" "$RUNTIME_USER"
  local desktop="$autostart/kallon-tower-dashboard.desktop"
  cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Sentinel
Comment=Local loopback console for bench tower (kiosk autostart)
Exec=${browser} --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --app=http://127.0.0.1:${TOWER_DASHBOARD_PORT}/
Icon=${icon_path}
X-GNOME-Autostart-enabled=true
OnlyShowIn=GNOME;Unity;XFCE;
EOF
  chown "$RUNTIME_USER:$RUNTIME_USER" "$desktop"
  chmod 0644 "$desktop"
  ok "installed kiosk autostart ($browser → http://127.0.0.1:${TOWER_DASHBOARD_PORT}/)"
}

main() {
  require_root
  load_env
  default_var ENABLE_TOWER_DASHBOARD 0
  default_var TOWER_DASHBOARD_PORT 8766
  default_var TOWER_STATUS_API_PORT 8770

  if [[ "${ENABLE_TOWER_DASHBOARD}" != "1" ]]; then
    disable_dashboard
    ok "tower dashboard disabled (ENABLE_TOWER_DASHBOARD=0)"
    return
  fi

  sync_dashboard_files
  write_mjpeg_proxy_unit
  write_dashboard_unit
  write_alert_listener_unit
  install_desktop_launcher
  install_kiosk_autostart

  systemctl daemon-reload

  # Each service reads device.env live (EnvironmentFile) and runs code synced
  # above, so restart only when its unit, device.env, or code changed. A plain
  # re-run leaves the dashboard/kiosk view untouched.
  local any_changed=0

  local mjpeg_changed=0
  if inputs_changed tower-mjpeg-proxy \
        /etc/systemd/system/kallon-tower-mjpeg-proxy.service \
        "$KALLON_ENV" \
        "$DASH_DIR/mjpeg_proxy.py"; then
    mjpeg_changed=1; any_changed=1
  fi
  apply_service_change "$mjpeg_changed" kallon-tower-mjpeg-proxy.service

  local dash_changed=0
  if inputs_changed tower-dashboard \
        /etc/systemd/system/kallon-tower-dashboard.service \
        "$KALLON_ENV" \
        "$DASH_DIR/gateway.py"; then
    dash_changed=1; any_changed=1
  fi
  apply_service_change "$dash_changed" kallon-tower-dashboard.service

  local alert_changed=0
  if inputs_changed tower-alert-listener \
        /etc/systemd/system/kallon-tower-alert-listener.service \
        "$KALLON_ENV" \
        "$APP_DIR/alert_listener.py"; then
    alert_changed=1; any_changed=1
  fi
  apply_service_change "$alert_changed" kallon-tower-alert-listener.service

  # When the dashboard is (re)configured, the watchdog needs a restart to pick
  # up TOWER_STATUS_API_* / ALERT_WEBHOOK_URL_LOCAL — but only if something here
  # actually changed, so we don't bounce it on every re-run.
  if [[ "$any_changed" == "1" ]] && systemctl is-active --quiet kallon-watchdog.service 2>/dev/null; then
    systemctl restart kallon-watchdog.service >/dev/null 2>&1 \
      && ok "kallon-watchdog restarted to pick up dashboard status API" \
      || warn "kallon-watchdog restart failed (status API may stay off until manual restart)."
  fi

  ok "tower dashboard module complete (http://127.0.0.1:${TOWER_DASHBOARD_PORT}/)"
}

main "$@"
