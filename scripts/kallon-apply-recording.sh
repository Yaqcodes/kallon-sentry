#!/usr/bin/env bash
# kallon-apply-recording.sh — persist continuous recording ON/OFF on a tower.
#
# Usage (root / sudo):
#   kallon-apply-recording on
#   kallon-apply-recording off
#
# Used by the tower dashboard gateway after it PATCH-es MediaMTX live.
# Updates RECORD_ENABLE in device.env and rewrites /etc/mediamtx.yml so the
# choice survives reboot and module-50 re-runs. Does NOT restart mediamtx
# (live state is already applied via the Control API).
#
set -euo pipefail

DEVICE_ENV="${KALLON_ENV:-/etc/kallon/device.env}"
MEDIAMTX_YML="${MEDIAMTX_YML:-/etc/mediamtx.yml}"
ENSURE_MOUNT="${ENSURE_MOUNT:-/usr/local/sbin/kallon-ensure-recordings-mount}"

log()  { printf '[kallon-recording] %s\n' "$*"; }
warn() { printf '[kallon-recording] WARN: %s\n' "$*" >&2; }
die()  { printf '[kallon-recording] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root"
[[ $# -eq 1 ]] || die "usage: $0 on|off"
case "$1" in
  on|1|true|yes)  WANT=1; VERB=on ;;
  off|0|false|no) WANT=0; VERB=off ;;
  *) die "usage: $0 on|off" ;;
esac

[[ -f "$DEVICE_ENV" ]] || die "missing $DEVICE_ENV"
[[ -f "$MEDIAMTX_YML" ]] || die "missing $MEDIAMTX_YML"

# Load device.env then resolve retention/segment via shared helper (SSOT).
# shellcheck disable=SC1090
set -a; source "$DEVICE_ENV"; set +a

export DEVICE_ENV
RESOLVED="$(python3 - <<'PY'
import json, os, sys
sys.path[:0] = ["/usr/local/lib/kallon", "/opt/kallon/tower-dashboard"]
from record_settings import resolve_record_settings
print(json.dumps(resolve_record_settings(device_env_path=os.environ.get("DEVICE_ENV", "/etc/kallon/device.env"))))
PY
)" || die "failed to resolve record settings (is record_settings.py installed under /usr/local/lib/kallon?)"

RECORD_PATH="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["record_path"])' "$RESOLVED")"
RECORD_MEDIAMTX_SEGMENT_FILE_DURATION="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["segment_duration"])' "$RESOLVED")"
# Effective value written to mediamtx.yml (0 when upload enabled — uploader owns deletes).
RECORD_MEDIAMTX_DELETE_AFTER="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["delete_after_effective"])' "$RESOLVED")"
log "device.env → segment=${RECORD_MEDIAMTX_SEGMENT_FILE_DURATION} delete_after(effective)=${RECORD_MEDIAMTX_DELETE_AFTER}"

# ── 1. Flip RECORD_ENABLE in device.env ──────────────────────────────────────
tmp="$(mktemp)"
if grep -qE '^[[:space:]]*RECORD_ENABLE=' "$DEVICE_ENV"; then
  sed -E "s|^[[:space:]]*RECORD_ENABLE=.*|RECORD_ENABLE=${WANT}|" "$DEVICE_ENV" > "$tmp"
else
  printf '\n# Toggled by kallon-apply-recording / dashboard\nRECORD_ENABLE=%s\n' "$WANT" \
    | cat "$DEVICE_ENV" - > "$tmp"
fi
# Preserve mode/owner.
mode="$(stat -c '%a' "$DEVICE_ENV" 2>/dev/null || echo 640)"
owner="$(stat -c '%u:%g' "$DEVICE_ENV" 2>/dev/null || echo '0:0')"
install -m "$mode" -o "${owner%:*}" -g "${owner#*:}" "$tmp" "$DEVICE_ENV"
rm -f "$tmp"
log "device.env RECORD_ENABLE=${WANT}"

# ── 2. Ensure SSD mount when enabling ────────────────────────────────────────
if [[ "$WANT" == "1" && -x "$ENSURE_MOUNT" ]]; then
  if "$ENSURE_MOUNT"; then
    log "recordings volume ready"
  else
    warn "ensure-recordings-mount failed — check SSD / LABEL=kallon-rec"
  fi
fi

# ── 3. Rewrite mediamtx.yml path recording flags (no process restart) ────────
export MEDIAMTX_YML RECORD_PATH RECORD_MEDIAMTX_SEGMENT_FILE_DURATION RECORD_MEDIAMTX_DELETE_AFTER
export WANT
python3 - <<'PY'
import os, re, sys, tempfile
from pathlib import Path

yml = Path(os.environ["MEDIAMTX_YML"])
want = os.environ["WANT"] == "1"
record_path = os.environ["RECORD_PATH"].rstrip("/")
seg = os.environ["RECORD_MEDIAMTX_SEGMENT_FILE_DURATION"]
delete_after = os.environ["RECORD_MEDIAMTX_DELETE_AFTER"]

text = yml.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

RECORD_KEYS = {
    "record",
    "recordPath",
    "recordFormat",
    "recordPartDuration",
    "recordSegmentDuration",
    "recordDeleteAfter",
}

def is_path_header(line: str) -> bool:
    return bool(re.match(r"^  cam\d+:\s*$", line))

def is_paths_end(line: str, in_path: bool) -> bool:
    if not in_path:
        return False
    # next top-level or next camN under paths
    if re.match(r"^[A-Za-z]", line):
        return True
    if is_path_header(line):
        return True
    return False

out: list[str] = []
i = 0
in_paths = False
while i < len(lines):
    line = lines[i]
    if re.match(r"^paths:\s*$", line):
        in_paths = True
        out.append(line)
        i += 1
        continue

    if in_paths and is_path_header(line):
        out.append(line)
        i += 1
        # collect body lines of this path (indent >= 4 spaces)
        body: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            if is_path_header(nxt) or (nxt.strip() and not nxt.startswith("    ") and not nxt.startswith("\t")):
                break
            if re.match(r"^[A-Za-z]", nxt):
                break
            body.append(nxt)
            i += 1

        # Drop old recording keys; keep everything else.
        kept: list[str] = []
        for b in body:
            m = re.match(r"^    ([A-Za-z][A-Za-z0-9_]*):", b)
            if m and m.group(1) in RECORD_KEYS:
                continue
            if m and m.group(1) == "sourceOnDemand":
                continue
            kept.append(b)

        # Ensure sourceOnDemand: off while recording; when stopping recording
        # leave whatever sourceOnDemand the live path already had (do not force
        # on-demand — that would drop the kiosk live feed).
        if want:
            on_demand = "no"
            kept.append(f"    sourceOnDemand: {on_demand}\n")
        else:
            # Preserve prior sourceOnDemand if present in original body.
            sod = None
            for b in body:
                m = re.match(r"^    sourceOnDemand:\s*(\S+)", b)
                if m:
                    sod = m.group(1)
            if sod is not None:
                kept.append(f"    sourceOnDemand: {sod}\n")

        if want:
            kept.extend([
                "    record: yes\n",
                f"    recordPath: {record_path}/%path/%Y-%m-%d_%H-%M-%S-%f\n",
                "    recordFormat: fmp4\n",
                "    recordPartDuration: 1s\n",
                f"    recordSegmentDuration: {seg}\n",
                f"    recordDeleteAfter: {delete_after}\n",
            ])
        else:
            kept.append("    record: no\n")

        # Dedupe blank-only trailing lines for neatness
        while kept and kept[-1].strip() == "":
            kept.pop()
        out.extend(kept)
        continue

    if in_paths and re.match(r"^[A-Za-z]", line):
        in_paths = False

    out.append(line)
    i += 1

new = "".join(out)
if new != text:
    mode = yml.stat().st_mode & 0o777
    fd, name = tempfile.mkstemp(prefix="mediamtx-", suffix=".yml", dir=str(yml.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        os.chmod(name, mode)
        os.replace(name, yml)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    print(f"updated {yml} record={'yes' if want else 'no'}", flush=True)
else:
    print(f"{yml} already matched record={'yes' if want else 'no'}", flush=True)
PY

log "recording ${VERB} persisted"
