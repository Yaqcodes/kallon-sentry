#!/usr/bin/env bash
# scripts/install/lib.sh
#
# Shared helpers for every Kallon installer module. Source this at the top of a
# module:
#
#   source "$(dirname "$0")/lib.sh"
#
# All modules are idempotent and safe to re-run. They read configuration from
# /etc/kallon/device.env (override with KALLON_ENV).

set -euo pipefail

KALLON_ENV="${KALLON_ENV:-/etc/kallon/device.env}"
KALLON_CONFIG_DIR="${KALLON_CONFIG_DIR:-/etc/kallon}"

# ── logging ──────────────────────────────────────────────────────────────────
_kallon_ts() { date '+%H:%M:%S'; }
log()   { printf '\033[0;36m[%s] %s\033[0m\n'  "$(_kallon_ts)" "$*"; }
ok()    { printf '\033[0;32m[%s] OK: %s\033[0m\n' "$(_kallon_ts)" "$*"; }
warn()  { printf '\033[0;33m[%s] WARN: %s\033[0m\n' "$(_kallon_ts)" "$*" >&2; }
err()   { printf '\033[0;31m[%s] ERROR: %s\033[0m\n' "$(_kallon_ts)" "$*" >&2; }
die()   { err "$*"; exit 1; }

# ── guards ───────────────────────────────────────────────────────────────────
require_root() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (use sudo)."
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# ── environment ──────────────────────────────────────────────────────────────
# Load device.env into the current shell, exporting every assignment.
load_env() {
  local env_file="${1:-$KALLON_ENV}"
  [[ -f "$env_file" ]] || die "device env not found: $env_file (copy deploy/device.env.example)"
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  ok "loaded $env_file"
}

# Fail if a required variable is empty/unset.
require_var() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "required variable $name is unset in $KALLON_ENV"
}

# Default a variable if unset (does not override).
default_var() {
  local name="$1" value="$2"
  if [[ -z "${!name:-}" ]]; then
    printf -v "$name" '%s' "$value"
    export "$name"
  fi
}

# ── idempotent file/dir helpers ──────────────────────────────────────────────
ensure_dir() {  # ensure_dir PATH MODE OWNER GROUP
  local path="$1" mode="${2:-0755}" owner="${3:-root}" group="${4:-root}"
  install -d -m "$mode" -o "$owner" -g "$group" "$path"
}

# Install a file only if content differs; returns 0 if changed, 1 if unchanged.
install_if_changed() {  # install_if_changed SRC DST MODE OWNER GROUP
  local src="$1" dst="$2" mode="${3:-0644}" owner="${4:-root}" group="${5:-root}"
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    log "unchanged: $dst"
    return 1
  fi
  install -m "$mode" -o "$owner" -g "$group" "$src" "$dst"
  ok "installed: $dst"
  return 0
}

# Run a module file by number prefix, e.g. run_module 30
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Comma-separated string → bash array. Usage: split_csv "$CAMERA_IPS" arr
split_csv() {
  local IFS=','
  # shellcheck disable=SC2206
  read -ra "$2" <<< "$1"
}
