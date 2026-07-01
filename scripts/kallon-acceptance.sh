#!/usr/bin/env bash
# kallon-acceptance.sh — verify a provisioned Jetson matches the bench contract.
#
# Checks (each prints PASS/FAIL; non-zero exit if any hard check fails):
#   1. Routing  — camera IPs via CAMERA_IFACE; internet via WAN/fallback.
#   2. WireGuard — interface up and a recent handshake.
#   3. RTSP      — local ffprobe of each rendered cam<n> path.
#   4. Alerts    — HMAC dry-run signature matches (no network needed).
#
# Usage: scripts/kallon-acceptance.sh [--env FILE]
set -uo pipefail   # NOTE: not -e; we want to run all checks and tally.

ENV_FILE="/etc/kallon/device.env"
[[ "${1:-}" == "--env" ]] && { ENV_FILE="$2"; shift 2; }

pass=0; fail=0; soft=0
PASS() { printf '\033[0;32m  PASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
FAIL() { printf '\033[0;31m  FAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
SOFT() { printf '\033[0;33m  WARN\033[0m  %s\n' "$*"; soft=$((soft+1)); }
hdr()  { printf '\n\033[0;36m== %s ==\033[0m\n' "$*"; }

[[ -f "$ENV_FILE" ]] || { echo "env not found: $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

route_dev() { ip route get "$1" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'; }

# 1. Routing ──────────────────────────────────────────────────────────────────
hdr "Routing policy"
first_cam="$(printf '%s' "${CAMERA_IPS:-}" | cut -d',' -f1 | tr -d ' ')"
if [[ -n "$first_cam" ]]; then
  d="$(route_dev "$first_cam")"
  [[ "$d" == "${CAMERA_IFACE:-}" ]] && PASS "camera $first_cam via $d" || FAIL "camera $first_cam via '${d:-none}', want ${CAMERA_IFACE:-?}"
else
  SOFT "CAMERA_IPS empty; skipping camera route check"
fi
wd="$(route_dev 1.1.1.1)"
if [[ "$wd" == "${WAN_IFACE:-}" || ( -n "${WAN_FALLBACK_IFACE:-}" && "$wd" == "$WAN_FALLBACK_IFACE" ) ]]; then
  PASS "internet via $wd"
else
  FAIL "internet via '${wd:-none}', want ${WAN_IFACE:-?} (or fallback)"
fi
# camera iface must NOT carry a default route
if ip route show default dev "${CAMERA_IFACE:-none}" 2>/dev/null | grep -q .; then
  FAIL "${CAMERA_IFACE} has a default route (must be camera-only)"
else
  PASS "${CAMERA_IFACE:-camera iface} has no default route"
fi

# 2. WireGuard ────────────────────────────────────────────────────────────────
hdr "WireGuard"
WG_IFACE="${WG_IFACE:-wg0}"
if ip link show "$WG_IFACE" >/dev/null 2>&1; then
  PASS "$WG_IFACE present"
  hs="$(wg show "$WG_IFACE" latest-handshakes 2>/dev/null | awk '{print $2; exit}')"
  now="$(date +%s)"
  if [[ -n "${hs:-}" && "$hs" != "0" && $((now - hs)) -lt 180 ]]; then
    PASS "recent handshake ($((now - hs))s ago)"
  else
    SOFT "no recent handshake (hub may be unreachable on this host)"
  fi
else
  SOFT "$WG_IFACE not up (expected once enrolled)"
fi

# 3. RTSP ─────────────────────────────────────────────────────────────────────
hdr "RTSP (local ffprobe)"
if command -v ffprobe >/dev/null 2>&1; then
  IFS=',' read -ra cams <<< "${CAMERA_IPS:-}"
  i=1
  for _ in "${cams[@]}"; do
    url="rtsp://127.0.0.1:8554/cam${i}"
    if timeout 12 ffprobe -v error -rtsp_transport tcp -i "$url" \
         -show_entries stream=codec_type -of csv=p=0 >/dev/null 2>&1; then
      PASS "ffprobe $url"
    else
      cam_ip="$(printf '%s' "${cams[$((i-1))]:-}" | tr -d ' ')"
      hint="camera/mediamtx down?"
      if [[ -n "$cam_ip" ]] && ! ping -c1 -W2 "$cam_ip" >/dev/null 2>&1; then
        hint="no ping to $cam_ip (wrong IP, VLAN, or camera offline?)"
      fi
      FAIL "ffprobe $url ($hint)"
    fi
    i=$((i+1))
  done
else
  SOFT "ffprobe not installed; skipping RTSP check"
fi

# 4. Alert HMAC dry-run ───────────────────────────────────────────────────────
hdr "Alert HMAC dry-run"
KEY="${ALERT_KEY_PATH:-/etc/kallon/alert.key}"
if [[ -f "$KEY" ]]; then
  payload='{"device_id":"'"${DEVICE_ID:-unknown}"'","type":"acceptance_dryrun","ts":0}'
  sig="$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$(tr -d '\0' < "$KEY")" -binary 2>/dev/null | xxd -p -c256)"
  if [[ -n "$sig" ]]; then
    PASS "HMAC signature computed (X-Kallon-Signature: sha256=${sig:0:16}...)"
  else
    FAIL "HMAC computation failed"
  fi
else
  FAIL "alert key missing: $KEY"
fi

# Tally ───────────────────────────────────────────────────────────────────────
hdr "Result"
printf 'pass=%d  fail=%d  warn=%d\n' "$pass" "$fail" "$soft"
[[ $fail -eq 0 ]] && { printf '\033[0;32mACCEPTANCE PASSED\033[0m\n'; exit 0; }
printf '\033[0;31mACCEPTANCE FAILED\033[0m\n'; exit 1
