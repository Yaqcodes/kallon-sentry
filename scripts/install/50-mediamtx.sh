#!/usr/bin/env bash
# 50-mediamtx.sh — install pinned mediamtx + render /etc/mediamtx.yml for N cameras.
#
# Renders one `cam<n>` path per entry in CAMERA_IPS, using CAMERA_RTSP_USER /
# CAMERA_PASSWORD / CAMERA_RTSP_PATH from device.env. The camera password lives
# only in /etc/mediamtx.yml (mode 0640 root:khalifa) and device.env.
#
# Idempotent.
source "$(dirname "$0")/lib.sh"

REPO_DIR="${REPO_DIR:-$(cd "$INSTALL_DIR/../.." && pwd)}"
MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-v1.9.3}"   # pinned; bump deliberately
MEDIAMTX_BIN=/usr/local/bin/mediamtx
MEDIAMTX_YML=/etc/mediamtx.yml

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
  curl -fSL "$url" -o "$tmp/$tar" || die "mediamtx download failed: $url"
  tar -xzf "$tmp/$tar" -C "$tmp" mediamtx
  install -m 0755 -o root -g root "$tmp/mediamtx" "$MEDIAMTX_BIN"
  rm -rf "$tmp"
  ok "installed mediamtx ${MEDIAMTX_VERSION}"
}

render_yml() {
  require_var CAMERA_IPS
  default_var CAMERA_RTSP_USER admin
  default_var CAMERA_RTSP_PATH '/cam/realmonitor?channel=1&subtype=1'
  default_var CAMERA_PASSWORD 'CAM_PASSWORD'

  local -a cams; split_csv "$CAMERA_IPS" cams
  local tmp; tmp="$(mktemp)"
  {
    echo "# /etc/mediamtx.yml — rendered by 50-mediamtx.sh (do not hand-edit)."
    echo "rtspAddress: :8554"
    echo "rtspTransports: [tcp]"
    echo "paths:"
    local i=1 ip
    for ip in "${cams[@]}"; do
      ip="${ip// /}"
      [[ -n "$ip" ]] || continue
      echo "  cam${i}:"
      echo "    source: rtsp://${CAMERA_RTSP_USER}:${CAMERA_PASSWORD}@${ip}:554${CAMERA_RTSP_PATH}"
      echo "    sourceOnDemand: yes"
      echo "    rtspTransport: tcp"
      i=$((i+1))
    done
  } > "$tmp"

  if [[ -f "$MEDIAMTX_YML" ]] && cmp -s "$tmp" "$MEDIAMTX_YML"; then
    log "mediamtx.yml unchanged"
    rm -f "$tmp"
  else
    install -m 0640 -o root -g khalifa "$tmp" "$MEDIAMTX_YML"
    rm -f "$tmp"
    ok "rendered $MEDIAMTX_YML for ${#cams[@]} camera(s)"
  fi
}

main() {
  require_root
  load_env
  install_binary
  render_yml
  install_if_changed "$REPO_DIR/deploy/mediamtx.service.example" /etc/systemd/system/mediamtx.service 0644 || true
  systemctl daemon-reload
  systemctl enable --now mediamtx.service >/dev/null 2>&1 || warn "mediamtx.service did not start (check cameras)."
  ok "mediamtx module complete"
}

main "$@"
