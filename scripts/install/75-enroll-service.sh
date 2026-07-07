#!/usr/bin/env bash
# 75-enroll-service.sh — install kallon-enroll.service (one-shot first-boot enrollment).
#
# Renders a unit that invokes kallon-enroll.sh via bash (repo tracks scripts 644).
# Idempotent.
source "$(dirname "$0")/lib.sh"

REPO_DIR="$(cd "$INSTALL_DIR/.." && pwd)"
ENROLL_SCRIPT="$REPO_DIR/kallon-enroll.sh"
ENROLL_UNIT=/etc/systemd/system/kallon-enroll.service
ENROLL_TIMER=/etc/systemd/system/kallon-enroll.timer

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

write_enroll_timer() {
  # Backstop retry: re-invokes kallon-enroll.service every few minutes until
  # /etc/kallon/.enrolled exists. Safe to leave enabled forever — the service's
  # own ConditionPathExists guard makes every post-enrollment tick a no-op.
  local tmp; tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Rendered by scripts/install/75-enroll-service.sh — do not hand-edit.
[Unit]
Description=Retry Kallon enrollment until it succeeds

[Timer]
OnBootSec=1min
OnUnitActiveSec=3min
AccuracySec=30s
Persistent=false

[Install]
WantedBy=timers.target
EOF
  install -m 0644 -o root -g root "$tmp" "$ENROLL_TIMER"
  rm -f "$tmp"
  ok "installed $ENROLL_TIMER"
}

main() {
  require_root
  load_env
  [[ -f "$ENROLL_SCRIPT" ]] || die "enroll script not found: $ENROLL_SCRIPT"
  write_enroll_unit
  write_enroll_timer
  systemctl daemon-reload
  systemctl enable kallon-enroll.service >/dev/null 2>&1 || true
  systemctl enable --now kallon-enroll.timer >/dev/null 2>&1 || true
  ok "kallon-enroll.service enabled (runs on boot); kallon-enroll.timer retries every few minutes until /etc/kallon/.enrolled exists"
}

main "$@"
