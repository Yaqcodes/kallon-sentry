#!/usr/bin/env bash
# 20-users-groups.sh — runtime user group membership + scoped sudoers.
#
# Idempotent: usermod -aG is additive; sudoers file written only if changed.
source "$(dirname "$0")/lib.sh"

main() {
  require_root
  load_env

  default_var RUNTIME_USER khalifa
  id -u "$RUNTIME_USER" >/dev/null 2>&1 || die "runtime user $RUNTIME_USER does not exist."

  # Hardware access groups for GPIO (reed/LDR), I2C (MPU-6050), camera devices.
  local grp
  for grp in gpio i2c video; do
    if getent group "$grp" >/dev/null; then
      if id -nG "$RUNTIME_USER" | tr ' ' '\n' | grep -qx "$grp"; then
        log "$RUNTIME_USER already in $grp"
      else
        usermod -aG "$grp" "$RUNTIME_USER"
        ok "added $RUNTIME_USER to $grp"
      fi
    else
      warn "group $grp not present on this host; skipping."
    fi
  done

  # Scoped sudoers: the watchdog/daemons need a few privileged ops without a
  # full sudo grant. Written atomically + validated with visudo -c.
  local sudoers=/etc/sudoers.d/kallon tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Managed by scripts/install/20-users-groups.sh — do not edit by hand.
$RUNTIME_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart wg-quick@wg0, \\
  /usr/bin/systemctl restart kallon-watchdog, \\
  /usr/bin/systemctl restart mediamtx, \\
  /usr/sbin/wg, /usr/bin/wg
EOF
  chmod 0440 "$tmp"
  if visudo -c -f "$tmp" >/dev/null; then
    if [[ -f "$sudoers" ]] && cmp -s "$tmp" "$sudoers"; then
      log "sudoers unchanged: $sudoers"
      rm -f "$tmp"
    else
      install -m 0440 -o root -g root "$tmp" "$sudoers"
      rm -f "$tmp"
      ok "installed scoped sudoers: $sudoers"
    fi
  else
    rm -f "$tmp"
    die "generated sudoers failed visudo validation."
  fi
}

main "$@"
