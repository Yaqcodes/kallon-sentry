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
# 0 = show steps + apt/pip progress (default). 1 = -qq/-q only. 2 = trace every command.
KALLON_INSTALL_QUIET="${KALLON_INSTALL_QUIET:-0}"

# ── logging ──────────────────────────────────────────────────────────────────
_kallon_ts() { date '+%H:%M:%S'; }
log()   { printf '\033[0;36m[%s] %s\033[0m\n'  "$(_kallon_ts)" "$*"; }
ok()    { printf '\033[0;32m[%s] OK: %s\033[0m\n' "$(_kallon_ts)" "$*"; }
warn()  { printf '\033[0;33m[%s] WARN: %s\033[0m\n' "$(_kallon_ts)" "$*" >&2; }
err()   { printf '\033[0;31m[%s] ERROR: %s\033[0m\n' "$(_kallon_ts)" "$*" >&2; }
die()   { err "$*"; exit 1; }

# ── install progress ─────────────────────────────────────────────────────────
_STEP_N=0

step_init() { _STEP_N=0; }

# Numbered sub-step within a module (e.g. "step 2/4: apt-get update").
step() {
  _STEP_N=$((_STEP_N + 1))
  log "step $_STEP_N: $*"
}

install_is_quiet() { [[ "${KALLON_INSTALL_QUIET:-0}" == "1" ]]; }
install_is_trace() { [[ "${KALLON_INSTALL_QUIET:-0}" == "2" ]]; }

# Run a command with optional trace banner; streams stdout/stderr unless quiet.
run_cmd() {
  local desc="$1"; shift
  if install_is_trace; then
    log "exec: $*"
    set -x
    "$@"
    { local ec=$?; set +x; return "$ec"; }
  fi
  log "$desc"
  if install_is_quiet; then
    "$@" >/dev/null
  else
    "$@"
  fi
}

# apt-get wrapper: quiet uses -qq; default shows index/download/dpkg progress.
apt_get() {
  local quiet=()
  install_is_quiet && quiet=(-qq)
  if install_is_trace; then
    log "exec: apt-get ${quiet[*]} $*"
    set -x
    apt-get "${quiet[@]}" "$@"
    { local ec=$?; set +x; return "$ec"; }
  fi
  log "apt-get $*"
  apt-get "${quiet[@]}" "$@"
}

# Report which apt packages are missing; sets APT_REPORT_MISSING to the count.
apt_report_packages() {
  APT_REPORT_MISSING=0
  local pkg
  for pkg in "$@"; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
      log "  present: $pkg"
    else
      log "  install: $pkg"
      APT_REPORT_MISSING=$((APT_REPORT_MISSING + 1))
    fi
  done
}

# pip3 wrapper: quiet uses -q; default shows download/install lines.
pip3_install() {
  local quiet=()
  install_is_quiet && quiet=(-q)
  if install_is_trace; then
    log "exec: pip3 install ${quiet[*]} $*"
    set -x
    pip3 install "${quiet[@]}" "$@"
    { local ec=$?; set +x; return "$ec"; }
  fi
  log "pip3 install $*"
  pip3 install "${quiet[@]}" "$@"
}

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
  # Who runs Kallon services. Prefer explicit device.env value; else the sudo
  # caller (typical factory SSH login); legacy bench default is khalifa.
  if [[ -z "${RUNTIME_USER:-}" ]]; then
    RUNTIME_USER="${SUDO_USER:-$(logname 2>/dev/null || echo khalifa)}"
    export RUNTIME_USER
  fi
  ok "loaded $env_file (RUNTIME_USER=$RUNTIME_USER)"
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
