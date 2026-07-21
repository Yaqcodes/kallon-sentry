#!/usr/bin/env bash
# Apply B2 recording upload settings to /etc/kallon/device.env and install uploader.
# Run ON the Jetson (not from git with secrets — pass vars via environment):
#
#   export AWS_ACCESS_KEY_ID='...'
#   export AWS_SECRET_ACCESS_KEY='...'
#   export RECORD_UPLOAD_INGEST_TOKEN='...'
#   sudo -E scripts/configure-recording-upload-on-tower.sh
#
# Optional overrides: S3_BUCKET, S3_ENDPOINT, S3_REGION, RECORD_UPLOAD_PLATFORM_URL
set -euo pipefail

ENV_FILE="${KALLON_ENV:-/etc/kallon/device.env}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

die() { echo "ERROR: $*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE"
[[ -n "${AWS_ACCESS_KEY_ID:-}" ]] || die "AWS_ACCESS_KEY_ID not set"
[[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] || die "AWS_SECRET_ACCESS_KEY not set"
[[ -n "${RECORD_UPLOAD_INGEST_TOKEN:-}" ]] || die "RECORD_UPLOAD_INGEST_TOKEN not set"

RUNTIME_USER="${SUDO_USER:-$(logname 2>/dev/null || id -un)}"
S3_BUCKET="${S3_BUCKET:-sentinel-recordings}"
S3_REGION="${S3_REGION:-us-east-005}"
S3_ENDPOINT="${S3_ENDPOINT:-https://s3.us-east-005.backblazeb2.com}"
S3_PREFIX="${S3_PREFIX:-}"
RECORD_UPLOAD_PLATFORM_URL="${RECORD_UPLOAD_PLATFORM_URL:-https://kallon-sentry-production.up.railway.app}"

upsert_env() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  cp "$ENV_FILE" "$tmp"
  python3 - "$tmp" "$key" "$val" <<'PY'
import sys
path, key, val = sys.argv[1:4]
lines = open(path, encoding="utf-8").read().splitlines()
out, seen = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={val}")
        seen = True
    else:
        out.append(line)
if not seen:
    if out and out[-1].strip():
        out.append("")
    out.append(f"{key}={val}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
  install -m 0640 -o root -g "$RUNTIME_USER" "$tmp" "$ENV_FILE"
  rm -f "$tmp"
}

echo "Updating $ENV_FILE for B2 upload..."
for kv in \
  "RECORD_UPLOAD_ENABLE=1" \
  "S3_BUCKET=$S3_BUCKET" \
  "S3_REGION=$S3_REGION" \
  "S3_ENDPOINT=$S3_ENDPOINT" \
  "S3_PREFIX=$S3_PREFIX" \
  "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" \
  "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY" \
  "RECORD_UPLOAD_INGEST_TOKEN=$RECORD_UPLOAD_INGEST_TOKEN" \
  "RECORD_UPLOAD_PLATFORM_URL=$RECORD_UPLOAD_PLATFORM_URL" \
  "RECORD_MEDIAMTX_SEGMENT_FILE_DURATION=15m"
do
  upsert_env "${kv%%=*}" "${kv#*=}"
done

cd "$REPO_DIR"
sudo -E env REPO_DIR="$REPO_DIR" scripts/kallon-jetson-install.sh --only-module 55
sudo pip3 install 'boto3>=1.34' 2>/dev/null || pip3 install --user 'boto3>=1.34'
sudo systemctl enable kallon-recording-uploader.service
sudo systemctl restart kallon-recording-uploader.service
echo "Uploader status:" && systemctl is-active kallon-recording-uploader.service
/usr/local/sbin/kallon-recording-uploader --once || true
echo "Done. Verify: curl -s http://127.0.0.1:8766/api/recording | python3 -m json.tool"
