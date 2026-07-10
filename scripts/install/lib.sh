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
  # caller (typical factory SSH login); else the logged-in terminal user.
  # Set RUNTIME_USER=<username> in device.env to be explicit on any image.
  if [[ -z "${RUNTIME_USER:-}" ]]; then
    RUNTIME_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
    [[ -n "$RUNTIME_USER" ]] || die "Cannot auto-detect RUNTIME_USER. Set RUNTIME_USER=<your-login> in $KALLON_ENV"
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

# ── applying config changes to services ──────────────────────────────────────
# Where we remember the inputs we last applied per service, so re-running a
# module only restarts a service when something it depends on actually changed.
KALLON_STATE_DIR="${KALLON_STATE_DIR:-/var/lib/kallon/applied}"

# inputs_changed TAG PATH...
# Returns 0 (true) if the combined content of the given files differs from the
# last time this TAG was applied (first run always counts as changed), else 1.
# Records the new hash so the next run is a no-op unless an input changes again.
# Use this to decide whether a service that reads device.env / app code / a
# rendered unit needs a restart — even when the change lives outside its own
# unit file (e.g. an edited device.env or updated /opt/kallon code).
inputs_changed() {  # inputs_changed TAG PATH...
  local tag="$1"; shift
  local stamp="$KALLON_STATE_DIR/${tag}.sha256" cur p
  cur="$(
    for p in "$@"; do
      [[ -f "$p" ]] && sha256sum "$p"
    done 2>/dev/null | sha256sum | awk '{print $1}'
  )"
  install -d -m 0755 "$KALLON_STATE_DIR" >/dev/null 2>&1 || true
  if [[ -f "$stamp" && "$(cat "$stamp" 2>/dev/null)" == "$cur" ]]; then
    return 1
  fi
  printf '%s\n' "$cur" > "$stamp" 2>/dev/null || true
  return 0
}

# apply_service_change CHANGED SERVICE...
# Bring services into line with freshly-rendered config so re-running a module
# (e.g. after editing device.env) needs no manual restart:
#   CHANGED=1 → restart so the new config takes effect (also starts if stopped).
#   CHANGED=0 → ensure enabled + running, WITHOUT a gratuitous restart.
apply_service_change() {  # apply_service_change CHANGED SERVICE...
  local changed="${1:-0}"; shift || true
  local svc
  for svc in "$@"; do
    systemctl enable "$svc" >/dev/null 2>&1 || true
    if [[ "$changed" == "1" ]]; then
      if systemctl restart "$svc" >/dev/null 2>&1; then
        ok "restarted $svc (config changed)"
      else
        warn "$svc failed to restart (check: journalctl -u $svc)"
      fi
    else
      if systemctl start "$svc" >/dev/null 2>&1; then
        log "$svc already current (config unchanged)"
      else
        warn "$svc failed to start (check: journalctl -u $svc)"
      fi
    fi
  done
}

# Run a module file by number prefix, e.g. run_module 30
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Comma-separated string → bash array. Usage: split_csv "$CAMERA_IPS" arr
split_csv() {
  local IFS=','
  # shellcheck disable=SC2206
  read -ra "$2" <<< "$1"
}
