#!/usr/bin/env bash
# Install pinned MediaMTX on a customer hub for HLS remux (loopback only).
#
# Used by kallon-gateway-init.sh and kallon-gateway-ensure-hls.sh.
# Idempotent.
set -euo pipefail

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-v1.11.3}"
MEDIAMTX_BIN=/usr/local/bin/mediamtx
MEDIAMTX_YML=/etc/mediamtx-hub.yml
MEDIAMTX_YML_SRC="${MEDIAMTX_YML_SRC:-}"
KALLON_DIR="${KALLON_DIR:-/opt/kallon-hub}"

log() { printf '[hub-mediamtx] %s\n' "$*"; }
die() { printf '[hub-mediamtx] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root."

install_binary() {
  if [[ -x "$MEDIAMTX_BIN" ]] && "$MEDIAMTX_BIN" --version 2>/dev/null | grep -q "${MEDIAMTX_VERSION#v}"; then
    log "mediamtx ${MEDIAMTX_VERSION} already installed"
    return
  fi
  local arch tar url tmp
  case "$(uname -m)" in
    aarch64|arm64) arch=arm64v8 ;;
    x86_64)        arch=amd64 ;;
    *) die "unsupported arch for mediamtx: $(uname -m)" ;;
  esac
  tar="mediamtx_${MEDIAMTX_VERSION}_linux_${arch}.tar.gz"
  url="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/${tar}"
  tmp="$(mktemp -d)"
  log "downloading mediamtx ${MEDIAMTX_VERSION} (${arch})"
  curl -fSL "$url" -o "$tmp/$tar" || die "download failed: $url"
  tar -xzf "$tmp/$tar" -C "$tmp" mediamtx
  install -m 0755 -o root -g root "$tmp/mediamtx" "$MEDIAMTX_BIN"
  rm -rf "$tmp"
  log "installed mediamtx ${MEDIAMTX_VERSION}"
}

install_config() {
  local src=""
  if [[ -n "$MEDIAMTX_YML_SRC" && -f "$MEDIAMTX_YML_SRC" ]]; then
    src="$MEDIAMTX_YML_SRC"
  else
    for _c in \
      "$(dirname "$0")/../infra/hub/mediamtx-hub.yml" \
      "$(dirname "$0")/infra/hub/mediamtx-hub.yml" \
      "$KALLON_DIR/mediamtx-hub.yml"; do
      [[ -f "$_c" ]] && { src="$_c"; break; }
    done
  fi
  [[ -n "$src" ]] || die "mediamtx-hub.yml not found (set MEDIAMTX_YML_SRC=...)"
  install -d -m 0755 "$(dirname "$MEDIAMTX_YML")"
  install -m 0644 "$src" "$MEDIAMTX_YML"
  # Keep a copy for ensure scripts.
  install -d -m 0755 "$KALLON_DIR"
  install -m 0644 "$src" "$KALLON_DIR/mediamtx-hub.yml"
  log "wrote $MEDIAMTX_YML"
}

install_unit() {
  cat > /etc/systemd/system/kallon-hub-mediamtx.service <<EOF
[Unit]
Description=Kallon hub MediaMTX (RTSP remux → local HLS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${MEDIAMTX_BIN} ${MEDIAMTX_YML}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now kallon-hub-mediamtx.service
  log "kallon-hub-mediamtx.service enabled"
}

install_binary
install_config
install_unit
log "OK: MediaMTX API http://127.0.0.1:9997  HLS http://127.0.0.1:8888"
