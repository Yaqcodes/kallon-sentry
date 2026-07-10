#!/usr/bin/env bash
# 30-network-policy.sh — the critical dual-NIC policy module.
#
# Enforces the WAN model:
#   * Default route ONLY on WAN interfaces, with metrics so Wi-Fi is preferred
#     over LTE (WAN_METRIC < WAN_FALLBACK_METRIC).
#   * CAMERA_IFACE carries the camera VLAN only and NEVER gets a default route.
#   * Each camera IP is pinned to CAMERA_IFACE (handled in detail by
#     60-camera-route.sh; here we assert the outcome).
#
# Installs /usr/local/sbin/kallon-wan-policy and a oneshot systemd unit that
# re-applies the policy and runs boot assertions on every boot.
#
# Idempotent: ip ... replace + install-if-changed.
source "$(dirname "$0")/lib.sh"

POLICY_BIN=/usr/local/sbin/kallon-wan-policy
POLICY_UNIT=/etc/systemd/system/kallon-wan-policy.service

write_policy_bin() {
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<'POLICY'
#!/usr/bin/env bash
# /usr/local/sbin/kallon-wan-policy — applied at boot by kallon-wan-policy.service
# and runnable on demand. Reads /etc/kallon/device.env.
set -euo pipefail
ENV_FILE="${KALLON_ENV:-/etc/kallon/device.env}"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${WAN_IFACE:?}"; : "${CAMERA_IFACE:?}"
WAN_METRIC="${WAN_METRIC:-100}"
WAN_FALLBACK_METRIC="${WAN_FALLBACK_METRIC:-700}"

log() { printf '[wan-policy] %s\n' "$*"; }

# 1. Camera interface: never a default route. Strip any default route on it.
while ip route show default dev "$CAMERA_IFACE" 2>/dev/null | grep -q .; do
  ip route del default dev "$CAMERA_IFACE" || break
  log "removed stray default route on $CAMERA_IFACE"
done

# 2. WAN primary: set/refresh its default route metric (preferred).
if ip link show "$WAN_IFACE" up >/dev/null 2>&1; then
  GW="$(ip route show default dev "$WAN_IFACE" 2>/dev/null | awk '/via/{print $3; exit}')"
  if [ -n "${GW:-}" ]; then
    ip route replace default via "$GW" dev "$WAN_IFACE" metric "$WAN_METRIC"
    log "default via $GW dev $WAN_IFACE metric $WAN_METRIC"
  fi
fi

# 3. WAN fallback (LTE): demote to a higher metric so it is only used if Wi-Fi drops.
if [ -n "${WAN_FALLBACK_IFACE:-}" ] && ip link show "$WAN_FALLBACK_IFACE" up >/dev/null 2>&1; then
  FGW="$(ip route show default dev "$WAN_FALLBACK_IFACE" 2>/dev/null | awk '/via/{print $3; exit}')"
  if [ -n "${FGW:-}" ]; then
    ip route replace default via "$FGW" dev "$WAN_FALLBACK_IFACE" metric "$WAN_FALLBACK_METRIC"
    log "fallback default via $FGW dev $WAN_FALLBACK_IFACE metric $WAN_FALLBACK_METRIC"
  fi
fi

# 4. Assertions — fail loudly if the policy is violated.
FIRST_CAM="$(printf '%s' "${CAMERA_IPS:-}" | cut -d',' -f1)"
rc=0
if [ -n "$FIRST_CAM" ]; then
  DEV="$(ip route get "$FIRST_CAM" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
  if [ "$DEV" = "$CAMERA_IFACE" ]; then
    log "ASSERT ok: $FIRST_CAM via $CAMERA_IFACE"
  else
    log "ASSERT FAIL: $FIRST_CAM routes via '${DEV:-none}', expected $CAMERA_IFACE"; rc=1
  fi
fi
WANDEV="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
if [ "$WANDEV" = "$WAN_IFACE" ] || [ "$WANDEV" = "${WAN_FALLBACK_IFACE:-}" ]; then
  log "ASSERT ok: internet via $WANDEV"
else
  log "ASSERT FAIL: internet routes via '${WANDEV:-none}', expected $WAN_IFACE (or fallback)"; rc=1
fi
exit $rc
POLICY
  install -m 0755 -o root -g root "$tmp" "$POLICY_BIN"
  rm -f "$tmp"
  ok "installed $POLICY_BIN"
}

write_policy_unit() {
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
[Unit]
Description=Kallon WAN/camera routing policy (Wi-Fi primary, LTE fallback, camera-only eth)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=KALLON_ENV=$KALLON_ENV
ExecStart=$POLICY_BIN

[Install]
WantedBy=multi-user.target
EOF
  install -m 0644 -o root -g root "$tmp" "$POLICY_UNIT"
  rm -f "$tmp"
  ok "installed $POLICY_UNIT"
}

main() {
  require_root
  load_env
  require_var WAN_IFACE
  require_var CAMERA_IFACE

  write_policy_bin
  write_policy_unit
  systemctl daemon-reload
  systemctl enable kallon-wan-policy.service >/dev/null 2>&1 || true

  # Apply now (best-effort; interfaces may not be up on a bench host).
  if KALLON_ENV="$KALLON_ENV" "$POLICY_BIN"; then
    ok "network policy applied and assertions passed"
  else
    warn "network policy applied but assertions failed (interfaces may be down on this host)."
  fi
}

main "$@"
