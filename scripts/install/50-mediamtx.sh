#!/usr/bin/env bash
# 50-mediamtx.sh — install pinned mediamtx + render /etc/mediamtx.yml for N cameras.
#
# Renders one `cam<n>` path per entry in CAMERA_IPS, using CAMERA_RTSP_USER /
# CAMERA_PASSWORD / CAMERA_RTSP_PATH from device.env. The camera password lives
# only in /etc/mediamtx.yml (mode 0640 root:khalifa) and device.env.
#
# The rendered config also enables the mediamtx Control API (127.0.0.1:9997)
# and HLS (127.0.0.1:8888) bound to loopback for the optional on-Jetson tower
# lab dashboard, and disables the unused RTMP/WebRTC servers. RTSP (:8554) is
# unchanged and remains the buyer/NOC surface (firewalled to lo + wg0 by
# module 90). These extra servers are loopback-only, so this is fleet-safe.
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

resolve_record_env() {
  # Deprecated aliases from early pilot docs — accept once, map to mediamtx field names.
  if [[ -n "${RECORD_SEGMENT_DURATION:-}" && -z "${RECORD_MEDIAMTX_SEGMENT_FILE_DURATION:-}" ]]; then
    RECORD_MEDIAMTX_SEGMENT_FILE_DURATION=$RECORD_SEGMENT_DURATION
    warn "RECORD_SEGMENT_DURATION is deprecated; use RECORD_MEDIAMTX_SEGMENT_FILE_DURATION"
  fi
  if [[ -n "${RECORD_RETENTION:-}" && -z "${RECORD_MEDIAMTX_DELETE_AFTER:-}" ]]; then
    RECORD_MEDIAMTX_DELETE_AFTER=$RECORD_RETENTION
    warn "RECORD_RETENTION is deprecated; use RECORD_MEDIAMTX_DELETE_AFTER"
  fi
  default_var RECORD_MEDIAMTX_SEGMENT_FILE_DURATION 1h
  default_var RECORD_MEDIAMTX_DELETE_AFTER 24h
}

render_yml() {
  require_var CAMERA_IPS
  default_var CAMERA_RTSP_USER admin
  default_var CAMERA_RTSP_PATH '/cam/realmonitor?channel=1&subtype=1'
  default_var CAMERA_PASSWORD 'CAM_PASSWORD'
  default_var RECORD_ENABLE 0
  default_var RECORD_PATH /var/kallon/recordings
  resolve_record_env

  local -a cams; split_csv "$CAMERA_IPS" cams

  # Recording requires a persistent source connection; live-only can be on-demand.
  local on_demand="yes"
  [[ "${RECORD_ENABLE}" == "1" ]] && on_demand="no"

  local tmp; tmp="$(mktemp)"
  {
    echo "# /etc/mediamtx.yml — rendered by 50-mediamtx.sh (do not hand-edit)."
    echo "rtspAddress: :8554"
    echo "protocols: [tcp]"
    echo "# Control API + HLS are bound to loopback (127.0.0.1) for the optional"
    echo "# on-Jetson tower lab dashboard only — never the network. The buyer/NOC"
    echo "# integration surface stays RTSP over wg0 (see docs/alert-webhook.md)."
    echo "# RTMP/WebRTC rebroadcasts are disabled: the Kallon stack does not use"
    echo "# them, and leaving them on would egress camera video on extra ports."
    echo "api: yes"
    echo "apiAddress: 127.0.0.1:9997"
    echo "hls: yes"
    echo "hlsAddress: 127.0.0.1:8888"
    echo "hlsVariant: fmp4"
    echo "rtmp: no"
    echo "webrtc: no"
    echo "paths:"
    local i=1 ip
    for ip in "${cams[@]}"; do
      ip="${ip// /}"
      [[ -n "$ip" ]] || continue
      echo "  cam${i}:"
      # Quote source — Dahua paths contain ? and & which break YAML if unquoted.
      echo "    source: \"rtsp://${CAMERA_RTSP_USER}:${CAMERA_PASSWORD}@${ip}:554${CAMERA_RTSP_PATH}\""
      echo "    sourceOnDemand: ${on_demand}"
      echo "    rtspTransport: tcp"
      if [[ "${RECORD_ENABLE}" == "1" ]]; then
        echo "    record: yes"
        echo "    recordPath: ${RECORD_PATH}/%path/%Y-%m-%d_%H-%M-%S-%f"
        echo "    recordFormat: fmp4"
        echo "    recordPartDuration: 1s"
        echo "    recordSegmentDuration: ${RECORD_MEDIAMTX_SEGMENT_FILE_DURATION}"
        echo "    recordDeleteAfter: ${RECORD_MEDIAMTX_DELETE_AFTER}"
      fi
      i=$((i+1))
    done
  } > "$tmp"

  if [[ -f "$MEDIAMTX_YML" ]] && cmp -s "$tmp" "$MEDIAMTX_YML"; then
    log "mediamtx.yml unchanged"
    rm -f "$tmp"
  else
    install -m 0640 -o root -g khalifa "$tmp" "$MEDIAMTX_YML"
    rm -f "$tmp"
    local rec_note=""
    [[ "${RECORD_ENABLE}" == "1" ]] && rec_note=" (recording → ${RECORD_PATH}, delete after ${RECORD_MEDIAMTX_DELETE_AFTER})"
    ok "rendered $MEDIAMTX_YML for ${#cams[@]} camera(s)${rec_note}"
  fi
}

main() {
  require_root
  load_env
  default_var RECORD_ENABLE 0
  default_var RECORD_PATH /var/kallon/recordings
  install_binary
  render_yml
  if [[ "${RECORD_ENABLE}" == "1" ]]; then
    if ! mountpoint -q "${RECORD_PATH}" 2>/dev/null; then
      warn "RECORD_ENABLE=1 but ${RECORD_PATH} is not a separate mountpoint — recordings will land on the OS partition."
    fi
    ensure_dir "${RECORD_PATH}" 0755 root root
    ok "recording directory ensured: ${RECORD_PATH}"
  fi
  install_if_changed "$REPO_DIR/deploy/mediamtx.service.example" /etc/systemd/system/mediamtx.service 0644 || true
  systemctl daemon-reload
  systemctl enable --now mediamtx.service >/dev/null 2>&1 || warn "mediamtx.service did not start (check cameras)."
  ok "mediamtx module complete"
}

main "$@"
