#!/bin/bash
# Restart wg-quick@wg0 if the latest WireGuard handshake is older than 60 s
# (or has never happened). Designed to be triggered by kallon-wg-watchdog.timer
# every 30 s — gives a sub-90-s recovery floor.
#
# Install as:
#   sudo install -m 0755 deploy/kallon-wg-watchdog.sh /usr/local/sbin/kallon-wg-watchdog.sh
#   sudo cp deploy/kallon-wg-watchdog.service.example /etc/systemd/system/kallon-wg-watchdog.service
#   sudo cp deploy/kallon-wg-watchdog.timer.example   /etc/systemd/system/kallon-wg-watchdog.timer
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now kallon-wg-watchdog.timer
set -euo pipefail
HS=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}')
NOW=$(date +%s)
if [ -z "$HS" ] || [ "$HS" = "0" ] || [ $((NOW - HS)) -gt 60 ]; then
  systemctl restart wg-quick@wg0
fi
