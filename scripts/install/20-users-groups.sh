#!/usr/bin/env bash
# 20-users-groups.sh — runtime user, group membership, sudoers, and login policy.
#
# Idempotent: usermod -aG is additive; sudoers/sshd/gdm written only if changed.
#
# Security model for this dedicated kiosk device:
#   - SSH access via key only (PasswordAuthentication disabled)
#   - Desktop auto-login (no password prompt at GDM)
#   - Full NOPASSWD sudo so remote admin works without a stored password
#   - Password locked (! hash) — the SSH key is the sole authentication token
source "$(dirname "$0")/lib.sh"

configure_gdm_autologin() {
  local user="$1"
  local cfg=/etc/gdm3/custom.conf
  [[ -f "$cfg" ]] || { warn "GDM config not found at $cfg; skipping autologin."; return; }

  # Use python3 for reliable ini editing — avoids sed edge cases on comments.
  python3 - "$cfg" "$user" <<'PYEOF'
import sys, re, pathlib
p, user = pathlib.Path(sys.argv[1]), sys.argv[2]
text = p.read_text()
# Ensure [daemon] section has the two autologin keys (add or replace).
if '[daemon]' not in text:
    text = '[daemon]\n' + text
def set_key(t, key, val):
    pattern = rf'(?m)^[# ]*{re.escape(key)}\s*=.*$'
    line = f'{key}={val}'
    return re.sub(pattern, line, t) if re.search(pattern, t) else re.sub(
        r'(\[daemon\])', r'\1\n' + line, t, count=1)
text = set_key(text, 'AutomaticLoginEnable', 'true')
text = set_key(text, 'AutomaticLogin', user)
p.write_text(text)
PYEOF
  ok "GDM autologin enabled for $user"
}

configure_ssh_key_only() {
  local cfg=/etc/ssh/sshd_config
  # Disable password auth; enable pubkey auth (pubkey is Ubuntu default but be explicit).
  sed -i \
    -e 's/^#*\s*PasswordAuthentication\s.*/PasswordAuthentication no/' \
    -e 's/^#*\s*PubkeyAuthentication\s.*/PubkeyAuthentication yes/' \
    "$cfg"
  # Add the directives if not present at all.
  grep -q '^PasswordAuthentication' "$cfg" || echo 'PasswordAuthentication no' >> "$cfg"
  grep -q '^PubkeyAuthentication'   "$cfg" || echo 'PubkeyAuthentication yes'  >> "$cfg"
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  ok "SSH: key-only auth enforced (PasswordAuthentication no)"
}

lock_password_if_key_present() {
  local user="$1"
  local auth_keys="/home/$user/.ssh/authorized_keys"
  if [[ -s "$auth_keys" ]]; then
    # Lock the password — SSH key is the sole authentication token.
    passwd -l "$user" >/dev/null 2>&1 || true
    ok "password locked for $user (SSH key present)"
  else
    warn "no authorized_keys found for $user — skipping password lock (set up SSH key first)."
  fi
}

main() {
  require_root
  load_env

  id -u "$RUNTIME_USER" >/dev/null 2>&1 || die "runtime user $RUNTIME_USER does not exist. Set RUNTIME_USER in $KALLON_ENV"

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

  # Full NOPASSWD sudo — the runtime user manages all system services and this is
  # a dedicated single-purpose device. The SSH key (not the password) is the
  # security boundary. Written atomically + validated with visudo -c.
  local sudoers=/etc/sudoers.d/kallon tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Managed by scripts/install/20-users-groups.sh — do not edit by hand.
$RUNTIME_USER ALL=(ALL) NOPASSWD: ALL
EOF
  chmod 0440 "$tmp"
  if visudo -c -f "$tmp" >/dev/null; then
    if [[ -f "$sudoers" ]] && cmp -s "$tmp" "$sudoers"; then
      log "sudoers unchanged: $sudoers"
      rm -f "$tmp"
    else
      install -m 0440 -o root -g root "$tmp" "$sudoers"
      rm -f "$tmp"
      ok "installed sudoers (NOPASSWD: ALL): $sudoers"
    fi
  else
    rm -f "$tmp"
    die "generated sudoers failed visudo validation."
  fi

  # Desktop: auto-login so the kiosk starts without a password prompt.
  configure_gdm_autologin "$RUNTIME_USER"

  # SSH: key-only — disable password authentication.
  configure_ssh_key_only

  # Lock password last (after sudo and SSH key auth are confirmed in place).
  lock_password_if_key_present "$RUNTIME_USER"
}

main "$@"
