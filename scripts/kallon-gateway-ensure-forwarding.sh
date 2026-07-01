#!/usr/bin/env bash
# kallon-gateway-ensure-forwarding.sh — idempotent hub VPN peer forwarding.
#
# In hub-and-spoke WireGuard, towers and NOC clients are separate peers. Traffic
# between them is routed through the hub (ip_forward). UFW must allow FORWARD
# on wg0 → wg0 or TCP services (RTSP :8554) fail while ICMP ping may still work.
#
# Fresh hubs get this from kallon-gateway-init.sh. Run this once on hubs
# provisioned before that fix, or after any UFW change that drops the route rule:
#
#   sudo bash scripts/kallon-gateway-ensure-forwarding.sh
#
# Safe to re-run; does not reset UFW or touch WireGuard peers.
set -euo pipefail

log() { printf '[gw-forward] %s\n' "$*"; }
die() { printf '[gw-forward] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root."

sysctl_file=/etc/sysctl.d/99-kallon-hub.conf
echo "net.ipv4.ip_forward = 1" > "$sysctl_file"
sysctl -p "$sysctl_file" >/dev/null
log "ip_forward enabled"

command -v ufw >/dev/null 2>&1 || die "ufw not installed"

ufw route allow in on wg0 out on wg0 >/dev/null 2>&1 || true
if ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw reload >/dev/null 2>&1 || true
fi
log "ufw route: allow in on wg0 out on wg0"

log "OK: VPN peer forwarding ensured (NOC ↔ tower over wg0)"
