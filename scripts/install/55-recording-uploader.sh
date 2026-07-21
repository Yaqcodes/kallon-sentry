#!/usr/bin/env bash
# 55-recording-uploader.sh — install tower S3 upload worker (upload-before-delete).
source "$(dirname "$0")/lib.sh"

REPO_DIR="${REPO_DIR:-$(cd "$INSTALL_DIR/../.." && pwd)}"
UPLOADER=/usr/local/sbin/kallon-recording-uploader
SERVICE=/etc/systemd/system/kallon-recording-uploader.service

install_uploader() {
  local src="$REPO_DIR/scripts/kallon-recording-uploader.py"
  [[ -f "$src" ]] || die "missing $src"
  install_if_changed "$src" "$UPLOADER" 0755 root root || true

  if command -v pip3 >/dev/null 2>&1; then
    log "ensuring boto3 for recording uploader"
    pip3 install --break-system-packages 'boto3>=1.34' >/dev/null 2>&1 \
      || pip3 install 'boto3>=1.34' >/dev/null 2>&1 \
      || warn "pip install boto3 failed — uploader will not start until boto3 is present"
  else
    warn "pip3 not found — install boto3 manually for recording uploads"
  fi

  local svc_tmp
  svc_tmp="$(mktemp)"
  sed \
    -e "s/__RUNTIME_USER__/${RUNTIME_USER}/g" \
    "$REPO_DIR/deploy/kallon-recording-uploader.service.example" > "$svc_tmp"
  install_if_changed "$svc_tmp" "$SERVICE" 0644 root root || true
  rm -f "$svc_tmp"

  systemctl daemon-reload
  if [[ "${RECORD_UPLOAD_ENABLE}" == "1" ]]; then
    systemctl enable kallon-recording-uploader.service >/dev/null 2>&1 || true
    systemctl restart kallon-recording-uploader.service >/dev/null 2>&1 || true
    ok "recording uploader enabled"
  else
    systemctl disable kallon-recording-uploader.service >/dev/null 2>&1 || true
    systemctl stop kallon-recording-uploader.service >/dev/null 2>&1 || true
    log "recording uploader installed but disabled (RECORD_UPLOAD_ENABLE=0)"
  fi
}

default_var RECORD_UPLOAD_ENABLE 0
install_uploader
