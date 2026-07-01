#!/usr/bin/env bash
# 60-camera-route.sh — pin each camera IP to CAMERA_IFACE at boot.
#
# Generalizes deploy/kallon-camera-route.service.example: builds the systemd
# oneshot from CAMERA_IFACE + CAMERA_JETSON_IP + CAMERA_IPS so the OS reaches
# cameras only over the wired NIC (never Wi-Fi), and the camera NIC keeps no
# default route.
#
# Idempotent.
source "$(dirname "$0")/lib.sh"

UNIT=/etc/systemd/system/kallon-camera-route.service

main() {
  require_root
  load_env
  require_var CAMERA_IFACE
  require_var CAMERA_JETSON_IP
  require_var CAMERA_IPS

  local -a cams; split_csv "$CAMERA_IPS" cams
  [[ ${#cams[@]} -gt 0 ]] || die "CAMERA_IPS is empty."

  local tmp; tmp="$(mktemp)"
  {
    cat <<EOF
# Rendered by scripts/install/60-camera-route.sh — do not hand-edit.
# Pins each camera /32 to $CAMERA_IFACE so camera traffic never uses Wi-Fi/WAN.
[Unit]
Description=Route camera IPs via direct Ethernet ($CAMERA_IFACE)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/sbin/ip link set $CAMERA_IFACE up
ExecStart=/sbin/ip addr replace $CAMERA_JETSON_IP dev $CAMERA_IFACE
EOF
    local ip
    for ip in "${cams[@]}"; do
      ip="${ip// /}"
      [[ -n "$ip" ]] || continue
      echo "ExecStart=/sbin/ip route replace ${ip}/32 dev $CAMERA_IFACE"
    done
    cat <<EOF

[Install]
WantedBy=multi-user.target
EOF
  } > "$tmp"

  if [[ -f "$UNIT" ]] && cmp -s "$tmp" "$UNIT"; then
    log "camera-route unit unchanged"
    rm -f "$tmp"
  else
    install -m 0644 -o root -g root "$tmp" "$UNIT"
    rm -f "$tmp"
    ok "rendered $UNIT for ${#cams[@]} camera(s)"
  fi

  systemctl daemon-reload
  systemctl enable kallon-camera-route.service >/dev/null 2>&1 || true
  systemctl restart kallon-camera-route.service || warn "camera-route service failed (iface $CAMERA_IFACE may be down)."
  # Module 50 may have started mediamtx before this unit assigned CAMERA_JETSON_IP.
  if systemctl is-enabled mediamtx.service &>/dev/null; then
    systemctl restart mediamtx.service >/dev/null 2>&1 \
      && ok "mediamtx restarted after camera routes applied" \
      || warn "mediamtx restart failed (check journalctl -u mediamtx)"
  fi
  ok "camera-route module complete"
}

main "$@"
