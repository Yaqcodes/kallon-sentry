#!/usr/bin/env bash
# 00-preflight.sh — verify the host can run the Kallon stack and the env is sane.
#
# Idempotent: read-only checks plus creation of /etc/kallon. Safe to re-run.
source "$(dirname "$0")/lib.sh"

main() {
  require_root

  # Architecture — production Jetson is arm64. Warn (don't fail) elsewhere so the
  # control-plane dev box can lint the scripts.
  local arch; arch="$(uname -m)"
  if [[ "$arch" != "aarch64" && "$arch" != "arm64" ]]; then
    warn "architecture is $arch, expected aarch64 (continuing — non-Jetson host)."
  else
    ok "architecture: $arch"
  fi

  load_env

  # Mandatory variables for a coherent install.
  local v
  for v in DEVICE_ID CUSTOMER_ID WAN_IFACE CAMERA_IFACE CAMERA_SUBNET \
           CAMERA_JETSON_IP CAMERA_IPS; do
    require_var "$v"
  done

  # Identity format sanity (cheap regex; full spec in docs/identity-and-secrets.md).
  [[ "$DEVICE_ID"   =~ ^kln_[a-z0-9]+_[0-9]{6}$ ]] || die "DEVICE_ID '$DEVICE_ID' not kln_<slug>_<6 digits>"
  [[ "$CUSTOMER_ID" =~ ^cust_[a-z0-9]+$ ]]          || die "CUSTOMER_ID '$CUSTOMER_ID' not cust_<slug>"

  # Interfaces must exist (camera + WAN). Fallback is optional.
  ip link show "$WAN_IFACE"    >/dev/null 2>&1 || warn "WAN_IFACE $WAN_IFACE not present yet."
  ip link show "$CAMERA_IFACE" >/dev/null 2>&1 || warn "CAMERA_IFACE $CAMERA_IFACE not present yet."

  ensure_dir "$KALLON_CONFIG_DIR" 0750 root khalifa
  ok "preflight passed for $DEVICE_ID ($CUSTOMER_ID)"
}

main "$@"
