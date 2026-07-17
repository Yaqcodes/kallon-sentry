#!/usr/bin/env bash
# Railway / container entrypoint for the Kallon enrollment + Platform API.
set -euo pipefail

mkdir -p /etc/kallon
chmod 700 /etc/kallon

IDENTITY_FILE="${KALLON_OPS_SSH_IDENTITY_FILE:-/etc/kallon/terra-hub-ops.pem}"

# Railway has no easy file mounts — materialize the ops PEM from env.
if [[ -n "${KALLON_OPS_SSH_IDENTITY_B64:-}" ]]; then
  printf '%s' "$KALLON_OPS_SSH_IDENTITY_B64" | base64 -d >"$IDENTITY_FILE"
  chmod 600 "$IDENTITY_FILE"
  echo "wrote ops SSH identity from KALLON_OPS_SSH_IDENTITY_B64 -> $IDENTITY_FILE"
elif [[ -n "${KALLON_OPS_SSH_IDENTITY:-}" ]]; then
  # Multiline PEM pasted into Railway env (use "New Variable" with newlines).
  printf '%s' "$KALLON_OPS_SSH_IDENTITY" >"$IDENTITY_FILE"
  # Ensure file ends with a newline (OpenSSH is picky).
  [[ -n "$(tail -c1 "$IDENTITY_FILE" | tr -d '\n' || true)" ]] && printf '\n' >>"$IDENTITY_FILE"
  chmod 600 "$IDENTITY_FILE"
  echo "wrote ops SSH identity from KALLON_OPS_SSH_IDENTITY -> $IDENTITY_FILE"
elif [[ -f "$IDENTITY_FILE" ]]; then
  echo "using existing ops SSH identity at $IDENTITY_FILE"
else
  echo "WARNING: no ops SSH identity — peer-add will fail until KALLON_OPS_SSH_IDENTITY_B64 (or _IDENTITY) is set" >&2
fi

export KALLON_OPS_SSH_IDENTITY_FILE="$IDENTITY_FILE"
export KALLON_REGISTRY="${KALLON_REGISTRY:-postgres}"
export KALLON_PEER_BACKEND="${KALLON_PEER_BACKEND:-subprocess}"
export KALLON_PROXY_VIA_HUB="${KALLON_PROXY_VIA_HUB:-1}"

# Optional one-shot schema apply (also available as Railway releaseCommand).
if [[ "${KALLON_INIT_SCHEMA:-0}" == "1" ]]; then
  echo "KALLON_INIT_SCHEMA=1 — running registry.cli init-schema"
  (cd /app && python -m registry.cli init-schema)
fi

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

cd /app/infra/enrollment-api
exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
