#!/usr/bin/env bash
# 10-packages.sh — install OS + Python dependencies for the Kallon stack.
#
# Idempotent: apt-get install is a no-op for already-present packages.
source "$(dirname "$0")/lib.sh"

APT_PACKAGES=(
  wireguard-tools          # wg, wg-quick
  wireguard-go             # userspace WG for Tegra (no in-tree module)
  ffmpeg                   # ffprobe for acceptance + health checks
  iptables                 # firewall rules
  iptables-persistent      # persist :8554 rules across reboot
  i2c-tools                # MPU-6050 bus debugging
  python3-pip
  python3-venv
  curl
  jq
)

main() {
  require_root
  export DEBIAN_FRONTEND=noninteractive

  log "apt-get update"
  apt-get update -qq

  log "installing: ${APT_PACKAGES[*]}"
  # iptables-persistent prompts unless preseeded.
  echo "iptables-persistent iptables-persistent/autosave_v4 boolean true"  | debconf-set-selections
  echo "iptables-persistent iptables-persistent/autosave_v6 boolean true"  | debconf-set-selections
  apt-get install -y -qq "${APT_PACKAGES[@]}" || die "apt install failed"

  # jetson-stats (jtop) is optional telemetry; never fail the build on it.
  if ! command -v jtop >/dev/null 2>&1; then
    log "installing jetson-stats (optional)"
    pip3 install -q jetson-stats || warn "jetson-stats install failed (optional, skipping)."
  fi

  ok "packages installed"
}

main "$@"
