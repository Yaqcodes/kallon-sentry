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
  step_init

  step "preseeding iptables-persistent (non-interactive debconf)"
  echo "iptables-persistent iptables-persistent/autosave_v4 boolean true"  | debconf-set-selections
  echo "iptables-persistent iptables-persistent/autosave_v6 boolean true"  | debconf-set-selections
  ok "debconf preseeded"

  step "refreshing package index (apt-get update — can take several minutes on first boot)"
  apt_get update || die "apt-get update failed"

  step "checking ${#APT_PACKAGES[@]} required packages"
  apt_report_packages "${APT_PACKAGES[@]}"
  if [[ "$APT_REPORT_MISSING" -eq 0 ]]; then
    ok "all required packages already installed"
  else
    step "installing $APT_REPORT_MISSING package(s) via apt (downloads + unpack — slow on fresh SD images)"
    apt_get install -y "${APT_PACKAGES[@]}" || die "apt install failed"
  fi

  # jetson-stats (jtop) is optional telemetry; never fail the build on it.
  if ! command -v jtop >/dev/null 2>&1; then
    step "installing jetson-stats / jtop (optional pip package)"
    pip3_install jetson-stats || warn "jetson-stats install failed (optional, skipping)."
  else
    log "jetson-stats already available ($(command -v jtop))"
  fi

  ok "packages installed"
}

main "$@"
