#!/usr/bin/env bash
# 70-app.sh — install the Kallon application code to /opt/kallon + Python deps.
#
# Copies the daemon code (watchdog, PTZ, ONVIF helpers) from the repo checkout
# into a stable runtime location and installs requirements.txt. Source is the
# repo root by default; override with REPO_DIR or KALLON_SRC.
#
# Idempotent.
source "$(dirname "$0")/lib.sh"

REPO_DIR="${REPO_DIR:-$(cd "$INSTALL_DIR/../.." && pwd)}"
KALLON_SRC="${KALLON_SRC:-$REPO_DIR}"
APP_DIR=/opt/kallon

APP_FILES=(
  kallon_watchdog.py
  kallon_ptz_daemon.py
  dahua_onvif_control.py
  sentry_ptz_absolute.py
  requirements.txt
)

main() {
  require_root
  load_env

  ensure_dir "$APP_DIR" 0755 root "$RUNTIME_USER"

  local f changed=0
  for f in "${APP_FILES[@]}"; do
    if [[ -f "$KALLON_SRC/$f" ]]; then
      if install_if_changed "$KALLON_SRC/$f" "$APP_DIR/$f" 0644 root "$RUNTIME_USER"; then
        changed=1
      fi
    else
      warn "source file missing, skipping: $KALLON_SRC/$f"
    fi
  done

  # wsdl/ directory is needed by the ONVIF stack.
  if [[ -d "$KALLON_SRC/wsdl" ]]; then
    cp -r "$KALLON_SRC/wsdl" "$APP_DIR/"
    chown -R root:"$RUNTIME_USER" "$APP_DIR/wsdl"
    log "synced wsdl/"
  fi

  if [[ -f "$APP_DIR/requirements.txt" ]]; then
    step "installing Python deps for $RUNTIME_USER (pip — can take a few minutes)"
    if install_is_quiet; then
      sudo -u "$RUNTIME_USER" pip3 install --user -q -r "$APP_DIR/requirements.txt" \
        || warn "pip install reported errors (continuing)."
    else
      sudo -u "$RUNTIME_USER" pip3 install --user -r "$APP_DIR/requirements.txt" \
        || warn "pip install reported errors (continuing)."
    fi
  fi

  ok "app installed to $APP_DIR (changed=$changed)"
}

main "$@"
