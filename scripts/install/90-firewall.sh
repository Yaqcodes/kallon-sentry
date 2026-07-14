#!/usr/bin/env bash
# 90-firewall.sh — restrict RTSP (:8554) and tower gateway (:8766) to lo + wg0.
#
# Applies iptables rules idempotently. NEVER touches SSH on the WAN iface.
source "$(dirname "$0")/lib.sh"

REPO_DIR="${REPO_DIR:-$(cd "$INSTALL_DIR/../.." && pwd)}"
TEMPLATE="$REPO_DIR/deploy/iptables-rebroadcast.rules.example"

main() {
  require_root
  load_env
  default_var WG_IFACE wg0
  default_var TOWER_DASHBOARD_PORT 8766
  require_cmd iptables

  [[ -f "$TEMPLATE" ]] || die "missing $TEMPLATE"

  # Apply discrete rules idempotently (insert only if absent) so we never clear
  # unrelated rules or risk locking out SSH.
  add_rule() {  # add_rule <iptables args...>
    if ! iptables -C "$@" 2>/dev/null; then
      iptables -A "$@"
      log "added: iptables -A $*"
    else
      log "present: iptables $*"
    fi
  }

  add_rule INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  add_rule INPUT -i lo -p tcp --dport 8554 -j ACCEPT
  add_rule INPUT -i "$WG_IFACE" -p tcp --dport 8554 -j ACCEPT
  add_rule INPUT -p tcp --dport 8554 -j DROP

  # Platform hub agent reaches gateway over WireGuard; keep SPA on loopback.
  add_rule INPUT -i lo -p tcp --dport "$TOWER_DASHBOARD_PORT" -j ACCEPT
  add_rule INPUT -i "$WG_IFACE" -p tcp --dport "$TOWER_DASHBOARD_PORT" -j ACCEPT
  add_rule INPUT -p tcp --dport "$TOWER_DASHBOARD_PORT" -j DROP

  # Persist (iptables-persistent / netfilter-persistent).
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1 || warn "netfilter-persistent save failed."
  elif [[ -d /etc/iptables ]]; then
    iptables-save > /etc/iptables/rules.v4
  else
    warn "no persistence backend; rules will not survive reboot."
  fi
  ok "firewall rules applied (8554 + ${TOWER_DASHBOARD_PORT} on lo + $WG_IFACE only)"
}

main "$@"
