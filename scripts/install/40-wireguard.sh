#!/usr/bin/env bash
# 40-wireguard.sh — userspace WireGuard for Tegra + wg0 + handshake watchdog.
#
# Does NOT generate keys or render wg0.conf — that is kallon-wg-provision.sh
# (run by the orchestrator / factory). This module installs the userspace
# drop-in, enables wg-quick@wg0, and the 30s handshake watchdog timer.
#
# Idempotent.
source "$(dirname "$0")/lib.sh"

REPO_DIR="${REPO_DIR:-$(cd "$INSTALL_DIR/../.." && pwd)}"
DROPIN_DIR=/etc/systemd/system/wg-quick@wg0.service.d
DROPIN=$DROPIN_DIR/userspace.conf

main() {
  require_root
  load_env
  default_var WG_IFACE wg0

  require_cmd wg
  require_cmd wg-quick

  # Userspace drop-in (mandatory on JetPack 6.x; harmless elsewhere only if the
  # wireguard-go binary exists). Skip on hosts with an in-tree module.
  if [[ -e /sys/module/wireguard ]]; then
    log "in-tree WireGuard module present; skipping userspace drop-in."
  else
    ensure_dir "$DROPIN_DIR" 0755 root root
    install_if_changed "$REPO_DIR/deploy/wg-quick-wg0-userspace.conf.example" "$DROPIN" 0644 || true
    systemctl daemon-reload
  fi

  # wg0.conf must already exist (rendered by kallon-wg-provision.sh).
  if [[ -f /etc/wireguard/wg0.conf ]]; then
    systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
    if systemctl restart wg-quick@wg0; then
      ok "wg-quick@wg0 up"
    else
      warn "wg-quick@wg0 failed to start (check rendered wg0.conf / endpoint)."
    fi
  else
    warn "/etc/wireguard/wg0.conf missing — run kallon-wg-provision.sh before enrollment."
  fi

  # Handshake watchdog (30s timer → restart if handshake older than 60s).
  install -m 0755 "$REPO_DIR/deploy/kallon-wg-watchdog.sh" /usr/local/sbin/kallon-wg-watchdog.sh
  install_if_changed "$REPO_DIR/deploy/kallon-wg-watchdog.service.example" /etc/systemd/system/kallon-wg-watchdog.service 0644 || true
  install_if_changed "$REPO_DIR/deploy/kallon-wg-watchdog.timer.example"   /etc/systemd/system/kallon-wg-watchdog.timer   0644 || true
  systemctl daemon-reload
  systemctl enable --now kallon-wg-watchdog.timer >/dev/null 2>&1 || true
  ok "wireguard module complete"
}

main "$@"
