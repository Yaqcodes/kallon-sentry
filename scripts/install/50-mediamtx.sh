#!/usr/bin/env bash
# 50-mediamtx.sh — install pinned mediamtx + render /etc/mediamtx.yml for N cameras.
#
# Renders one `cam<n>` path per entry in CAMERA_IPS, using CAMERA_RTSP_USER /
# CAMERA_PASSWORD / CAMERA_RTSP_PATH from device.env. The camera password lives
# only in /etc/mediamtx.yml (mode 0640 root:$RUNTIME_USER) and device.env.
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
MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-v1.11.3}"   # pinned; bump deliberately
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
  log "downloading mediamtx ${MEDIAMTX_VERSION} (${arch}) from GitHub"
  run_cmd "curl download: $tar" curl -fSL "$url" -o "$tmp/$tar" || die "mediamtx download failed: $url"
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
  # Bare numbers (e.g. "24") are ambiguous — mediamtx expects a Go duration like 24h.
  if [[ "${RECORD_MEDIAMTX_DELETE_AFTER}" =~ ^[0-9]+$ ]]; then
    RECORD_MEDIAMTX_DELETE_AFTER="${RECORD_MEDIAMTX_DELETE_AFTER}h"
    warn "RECORD_MEDIAMTX_DELETE_AFTER had no unit; assuming hours (${RECORD_MEDIAMTX_DELETE_AFTER})"
  fi
}

# Canonical recordings path. mediamtx ALWAYS uses this path — never /media/...
# NVMe detection / reclaim / fstab is handled by kallon-ensure-recordings-mount
# (run at install time and again on every boot via systemd).
resolve_record_path() {
  default_var RECORD_PATH /var/kallon/recordings
  # Operators may set RECORD_PATH in device.env, but keep it a single fixed
  # location so dashboards / operators always know where segments live.
  export RECORD_PATH
}

install_recordings_mount_helper() {
  local src="$REPO_DIR/scripts/kallon-ensure-recordings-mount.sh"
  local dst=/usr/local/sbin/kallon-ensure-recordings-mount
  [[ -f "$src" ]] || die "missing $src"
  install_if_changed "$src" "$dst" 0755 root root || true
  install_if_changed \
    "$REPO_DIR/deploy/kallon-recordings-mount.service.example" \
    /etc/systemd/system/kallon-recordings-mount.service \
    0644 root root || true
  systemctl daemon-reload
  systemctl enable kallon-recordings-mount.service >/dev/null 2>&1 || true
}

# Dashboard / platform toggle: persist RECORD_ENABLE + rewrite mediamtx.yml.
install_recording_apply_helper() {
  local src="$REPO_DIR/scripts/kallon-apply-recording.sh"
  local dst=/usr/local/sbin/kallon-apply-recording
  local sudotmp sudodst=/etc/sudoers.d/kallon-recording
  [[ -f "$src" ]] || die "missing $src"
  install_if_changed "$src" "$dst" 0755 root root || true

  if [[ -f "$REPO_DIR/deploy/kallon-recording.sudoers.example" ]]; then
    sudotmp="$(mktemp)"
    sed "s/__RUNTIME_USER__/${RUNTIME_USER}/g" \
      "$REPO_DIR/deploy/kallon-recording.sudoers.example" > "$sudotmp"
    if [[ -f "$sudodst" ]] && cmp -s "$sudotmp" "$sudodst"; then
      log "unchanged: $sudodst"
      rm -f "$sudotmp"
    else
      if visudo -cf "$sudotmp" >/dev/null 2>&1; then
        install -m 0440 -o root -g root "$sudotmp" "$sudodst"
        ok "installed: $sudodst (NOPASSWD ${RUNTIME_USER} → kallon-apply-recording)"
      else
        warn "sudoers fragment failed visudo -cf — left $sudodst untouched"
      fi
      rm -f "$sudotmp"
    fi
  fi
}

# Mount (or confirm) the SSD at RECORD_PATH before rendering mediamtx.yml.
ensure_recordings_volume() {
  resolve_record_path
  # Always install apply helpers so the dashboard can enable recording later.
  install_recording_apply_helper
  install_recordings_mount_helper
  if [[ "${RECORD_ENABLE}" != "1" ]]; then
    return 0
  fi
  if /usr/local/sbin/kallon-ensure-recordings-mount; then
    ok "recordings volume ready at ${RECORD_PATH}"
  else
    warn "kallon-ensure-recordings-mount failed — mediamtx may write on the OS disk"
  fi
  local src
  src="$(findmnt -n -o SOURCE --target "${RECORD_PATH}" 2>/dev/null || true)"
  if [[ "$src" == /dev/nvme* ]]; then
    ok "recordings backed by NVMe (${src})"
  elif findmnt -n -o SOURCE / 2>/dev/null | grep -q '^/dev/nvme'; then
    ok "OS root is NVMe — recordings on ${RECORD_PATH} are on SSD"
  else
    warn "RECORD_ENABLE=1 but ${RECORD_PATH} is not on NVMe (source=${src:-unknown}) — check SSD / LABEL=kallon-rec"
  fi
}

render_yml() {
  require_var CAMERA_IPS
  default_var CAMERA_RTSP_USER admin
  # Quote values in device.env — bare & breaks bash `source` (background op).
  default_var CAMERA_RTSP_PATH '/cam/realmonitor?channel=1&subtype=0'
  # Buyer HLS / hub remux uses the substream path (camN_sub). Falls back to
  # swapping subtype=0→1 when CAMERA_RTSP_PATH_SUB is unset.
  default_var CAMERA_RTSP_PATH_SUB ''
  default_var CAMERA_PASSWORD 'CAM_PASSWORD'
  default_var RECORD_ENABLE 0
  resolve_record_path
  resolve_record_env

  local sub_path="${CAMERA_RTSP_PATH_SUB}"
  if [[ -z "$sub_path" ]]; then
    if [[ "$CAMERA_RTSP_PATH" == *subtype=0* ]]; then
      sub_path="${CAMERA_RTSP_PATH/subtype=0/subtype=1}"
    else
      sub_path="$CAMERA_RTSP_PATH"
    fi
  fi

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
    echo "# camN = main (NVR/NOC); camN_sub = low-bitrate for hub HLS remux."
    echo "api: yes"
    echo "apiAddress: 127.0.0.1:9997"
    echo "hls: yes"
    echo "hlsAddress: 127.0.0.1:8888"
    echo "hlsVariant: lowLatency"
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
      # Substream for buyer live (hub remux). Always on-demand; never recorded.
      echo "  cam${i}_sub:"
      echo "    source: \"rtsp://${CAMERA_RTSP_USER}:${CAMERA_PASSWORD}@${ip}:554${sub_path}\""
      echo "    sourceOnDemand: yes"
      echo "    rtspTransport: tcp"
      i=$((i+1))
    done
  } > "$tmp"

  if [[ -f "$MEDIAMTX_YML" ]] && cmp -s "$tmp" "$MEDIAMTX_YML"; then
    log "mediamtx.yml unchanged"
    rm -f "$tmp"
  else
    install -m 0640 -o root -g "${RUNTIME_USER}" "$tmp" "$MEDIAMTX_YML"
    rm -f "$tmp"
    local rec_note=""
    [[ "${RECORD_ENABLE}" == "1" ]] && rec_note=" (recording → ${RECORD_PATH}, delete after ${RECORD_MEDIAMTX_DELETE_AFTER})"
    ok "rendered $MEDIAMTX_YML for ${#cams[@]} camera(s) + ${#cams[@]} sub path(s)${rec_note}"
  fi
}

main() {
  require_root
  load_env
  default_var RECORD_ENABLE 0
  ensure_recordings_volume
  install_binary
  render_yml
  if [[ "${RECORD_ENABLE}" == "1" ]]; then
    ensure_dir "${RECORD_PATH}" 0755 root root
    ok "recording directory ensured: ${RECORD_PATH}"
  fi
  install_if_changed "$REPO_DIR/deploy/mediamtx.service.example" /etc/systemd/system/mediamtx.service 0644 || true
  systemctl daemon-reload

  # Restart only when the binary, rendered config, or unit actually changed, so
  # re-running the module never drops a live RTSP stream for nothing.
  local changed=0
  if inputs_changed mediamtx "$MEDIAMTX_BIN" "$MEDIAMTX_YML" /etc/systemd/system/mediamtx.service \
       /etc/systemd/system/kallon-recordings-mount.service \
       /usr/local/sbin/kallon-ensure-recordings-mount; then
    changed=1
  fi
  if [[ "${RECORD_ENABLE}" == "1" ]]; then
    apply_service_change 1 kallon-recordings-mount.service
  fi
  apply_service_change "$changed" mediamtx.service
  ok "mediamtx module complete"
}

main "$@"
