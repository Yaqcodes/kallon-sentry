#!/usr/bin/env bash
# 75-enroll-service.sh — install kallon-enroll.service (one-shot first-boot enrollment).
#
# Renders a unit that invokes kallon-enroll.sh via bash (repo tracks scripts 644).
# Idempotent.
source "$(dirname "$0")/lib.sh"

REPO_DIR="$(cd "$INSTALL_DIR/.." && pwd)"
ENROLL_SCRIPT="$REPO_DIR/kallon-enroll.sh"
ENROLL_UNIT=/etc/systemd/system/kallon-enroll.service

write_enroll_unit() {
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Rendered by scripts/install/75-enroll-service.sh — do not hand-edit.
[Unit]
Description=Kallon first-boot enrollment
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/etc/kallon/.enrolled

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=KALLON_ENV=$KALLON_ENV
ExecStart=/usr/bin/bash $ENROLL_SCRIPT --env $KALLON_ENV

[Install]
WantedBy=multi-user.target
EOF
  install -m 0644 -o root -g root "$tmp" "$ENROLL_UNIT"
  rm -f "$tmp"
  ok "installed $ENROLL_UNIT"
}

main() {
  require_root
  load_env
  [[ -f "$ENROLL_SCRIPT" ]] || die "enroll script not found: $ENROLL_SCRIPT"
  write_enroll_unit
  systemctl daemon-reload
  systemctl enable kallon-enroll.service >/dev/null 2>&1 || true
  ok "kallon-enroll.service enabled (runs on boot until /etc/kallon/.enrolled exists)"
}

main "$@"
