#!/usr/bin/env bash
# kallon-jetson-install.sh — single entry point to provision a Jetson.
#
# Prerequisite: /etc/kallon/device.env must exist before the first run.
#   See docs/identity-and-secrets.md §3.2 (create /etc/kallon/, install device.env
#   and alert.key with mode 0640).
#
# Runs the ordered, idempotent modules in scripts/install/ (00 → 99). Safe to
# re-run. Each module reads /etc/kallon/device.env (override with --env).
#
# Usage:
#   sudo scripts/kallon-jetson-install.sh [--env FILE]
#                                         [--only-module N[,N...]]
#                                         [--skip-module N[,N...]]
#                                         [--quiet] [--verbose]
#                                         [--list] [--dry-run]
#
# Examples:
#   sudo scripts/kallon-jetson-install.sh                      # full install
#   sudo scripts/kallon-jetson-install.sh --only-module 30,60  # just networking
#   sudo scripts/kallon-jetson-install.sh --skip-module 99     # install, no gate
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$SCRIPT_DIR/install"
ENV_FILE="/etc/kallon/device.env"
ONLY=""
SKIP=""
DRY_RUN=0
INSTALL_QUIET=0

c() { printf '\033[0;36m%s\033[0m\n' "$*"; }
g() { printf '\033[0;32m%s\033[0m\n' "$*"; }
r() { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

module_num() { basename "$1" | grep -oE '^[0-9]+'; }

module_desc() {
  sed -n '2s/^# [0-9][0-9]*-[^—]*— //p' "$1" 2>/dev/null || true
}

fmt_elapsed() {
  local s="$1" m h
  if (( s >= 3600 )); then
    h=$((s / 3600)); m=$(((s % 3600) / 60)); s=$((s % 60))
    printf '%dh%02dm%02ds' "$h" "$m" "$s"
  elif (( s >= 60 )); then
    m=$((s / 60)); s=$((s % 60))
    printf '%dm%02ds' "$m" "$s"
  else
    printf '%ds' "$s"
  fi
}

list_modules() {
  local m
  for m in "$INSTALL_DIR"/[0-9]*.sh; do
    printf '  %s  %s\n' "$(module_num "$m")" "$(basename "$m")"
  done
}

in_csv() {  # in_csv <num> <csv>
  local n="$1" csv="$2" x IFS=','
  for x in $csv; do [[ "${x// /}" == "$n" ]] && return 0; done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)          ENV_FILE="$2"; shift 2 ;;
    --only-module)  ONLY="$2"; shift 2 ;;
    --skip-module)  SKIP="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --quiet|-q)     INSTALL_QUIET=1; shift ;;
    --verbose|-v)   INSTALL_QUIET=2; shift ;;
    --list)         list_modules; exit 0 ;;
    -h|--help)      usage 0 ;;
    *) r "unknown arg: $1"; usage 1 ;;
  esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || { r "must run as root (sudo)."; exit 1; }
[[ -d "$INSTALL_DIR" ]] || { r "install dir not found: $INSTALL_DIR"; exit 1; }

export KALLON_ENV="$ENV_FILE"
export KALLON_INSTALL_QUIET="$INSTALL_QUIET"

c "Kallon Jetson installer"
c "  env:     $ENV_FILE"
c "  only:    ${ONLY:-<all>}"
c "  skip:    ${SKIP:-<none>}"
case "$INSTALL_QUIET" in
  0) c "  output:  progress (apt/pip visible; use --quiet to suppress)" ;;
  1) c "  output:  quiet" ;;
  2) c "  output:  verbose (command trace)" ;;
esac
echo

failed=()
for module in "$INSTALL_DIR"/[0-9]*.sh; do
  num="$(module_num "$module")"
  if [[ -n "$ONLY" ]] && ! in_csv "$num" "$ONLY"; then continue; fi
  if [[ -n "$SKIP" ]] && in_csv "$num" "$SKIP"; then c ">> skip module $num"; continue; fi

  desc="$(module_desc "$module")"
  c "========================================================================"
  c ">> module $num : $(basename "$module")"
  [[ -n "$desc" ]] && c "   $desc"
  c "========================================================================"
  if [[ $DRY_RUN -eq 1 ]]; then
    g "   (dry-run) would execute $module"
    continue
  fi
  mod_start=$SECONDS
  if bash "$module"; then
    g ">> module $num OK ($(fmt_elapsed $((SECONDS - mod_start))))"
  else
    r ">> module $num FAILED after $(fmt_elapsed $((SECONDS - mod_start)))"
    failed+=("$num")
    # The acceptance gate is allowed to fail the whole run; others stop early too.
    break
  fi
  echo
done

if [[ ${#failed[@]} -gt 0 ]]; then
  r "install FAILED at module(s): ${failed[*]}"
  exit 1
fi
g "install complete."
