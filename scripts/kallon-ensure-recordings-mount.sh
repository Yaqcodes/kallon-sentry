#!/usr/bin/env bash
# kallon-ensure-recordings-mount.sh — put continuous recordings on SSD when present.
#
# Contract:
#   - mediamtx always writes to RECORD_PATH (default /var/kallon/recordings).
#   - When a dedicated/writable NVMe recording volume exists, that path MUST be
#     a mount of that volume — never a directory on the OS root SD card.
#   - Works whether the Jetson image boots from SD (mmcblk) or NVMe:
#       * LABEL=kallon-rec              → mount at RECORD_PATH (preferred)
#       * other non-root NVMe partition → reclaim + mount at RECORD_PATH
#       * OS already on NVMe, no extra partition → RECORD_PATH on root is fine
#   - Idempotent. Safe to run from the installer and as a systemd oneshot
#     Before=mediamtx.service.
#
# Does NOT auto-format blank disks (destructive). Partition + label once:
#   sudo mkfs.ext4 -L kallon-rec /dev/nvme0n1p1
#
set -euo pipefail

RECORD_PATH="${RECORD_PATH:-/var/kallon/recordings}"
RECORD_LABEL="${RECORD_LABEL:-kallon-rec}"
FSTAB=/etc/fstab
STATE_DIR=/var/lib/kallon
STATE_FILE="${STATE_DIR}/recordings-mount.env"

log()  { printf '[kallon-rec] %s\n' "$*"; }
warn() { printf '[kallon-rec] WARN: %s\n' "$*" >&2; }
die()  { printf '[kallon-rec] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root"
}

root_source() {
  findmnt -n -o SOURCE / 2>/dev/null | head -1
}

# Resolve /dev/nvme0n1p1 → nvme0n1 (parent disk name).
disk_of() {
  local src="$1" base pk
  base="$(basename "$src")"
  base="${base%%\[*}"   # drop [mapper] noise
  pk="$(lsblk -no PKNAME "/dev/${base}" 2>/dev/null | head -1 || true)"
  if [[ -n "$pk" ]]; then
    printf '%s\n' "$pk"
  else
    # Already a whole disk, or lsblk failed.
    printf '%s\n' "$base"
  fi
}

is_nvme_dev() {
  [[ "$1" == /dev/nvme* ]]
}

fstype_of() {
  blkid -o value -s TYPE "$1" 2>/dev/null || true
}

ensure_dir() {
  install -d -m 0755 -o root -g root "$RECORD_PATH"
}

# Return 0 if TARGET is already backed by an NVMe block device (mounted or root).
path_on_nvme() {
  local src
  src="$(findmnt -n -o SOURCE --target "$1" 2>/dev/null | head -1 || true)"
  [[ -n "$src" ]] && is_nvme_dev "$src"
}

# Unmount every mount of DEV except RECORD_PATH (reclaim from GNOME/udisks).
reclaim_device() {
  local dev="$1" tgt
  while IFS= read -r tgt; do
    [[ -z "$tgt" || "$tgt" == "$RECORD_PATH" ]] && continue
    log "unmounting ${dev} from ${tgt} (reclaim for ${RECORD_PATH})"
    umount "$tgt" 2>/dev/null || umount -l "$tgt" 2>/dev/null || \
      warn "could not unmount ${tgt}"
  done < <(findmnt -nr -S "$dev" -o TARGET 2>/dev/null || true)
}

# Persist LABEL= → RECORD_PATH in fstab (idempotent replace).
ensure_fstab() {
  local dev="$1" uuid line tmp
  uuid="$(blkid -o value -s UUID "$dev" 2>/dev/null || true)"
  if [[ -z "$uuid" ]]; then
    warn "no UUID for ${dev}; skipping fstab"
    return 0
  fi
  line="UUID=${uuid}  ${RECORD_PATH}  ext4  defaults,nofail,noatime,x-systemd.device-timeout=10,x-gvfs-hide  0  2"
  tmp="$(mktemp)"
  if [[ -f "$FSTAB" ]]; then
    # Drop any previous RECORD_PATH or kallon-rec / this UUID lines.
    grep -vE "([[:space:]]${RECORD_PATH}[[:space:]]|LABEL=${RECORD_LABEL}[[:space:]]|UUID=${uuid}[[:space:]])" \
      "$FSTAB" > "$tmp" || true
  else
    : > "$tmp"
  fi
  printf '%s\n' "$line" >> "$tmp"
  if ! cmp -s "$tmp" "$FSTAB" 2>/dev/null; then
    install -m 0644 -o root -g root "$tmp" "$FSTAB"
    log "fstab: ${line}"
  else
    log "fstab already correct for ${RECORD_PATH}"
  fi
  rm -f "$tmp"
}

# Best dedicated recording device, or empty if none (OS-on-NVMe uses root).
find_recording_device() {
  local root_src root_disk cand size best="" best_size=0
  root_src="$(root_source)"
  root_disk="$(disk_of "$root_src")"

  # 1) Explicit Kallon label wins always.
  cand="$(blkid -L "$RECORD_LABEL" 2>/dev/null || true)"
  if [[ -n "$cand" && -b "$cand" ]]; then
    if [[ "$cand" == "$root_src" ]]; then
      warn "LABEL=${RECORD_LABEL} is the root filesystem — using root (already OS volume)"
      return 1
    fi
    printf '%s\n' "$cand"
    return 0
  fi

  # 2) Largest formatted NVMe volume that is not root (data SSD while OS on SD,
  #    or a second partition when OS lives on another NVMe slice). Includes
  #    whole-disk filesystems (nvme0n1 with no partition table).
  local dev pk fst
  while IFS= read -r dev; do
    [[ -b "$dev" ]] || continue
    [[ "$dev" == "$root_src" ]] && continue
    pk="$(disk_of "$dev")"
    fst="$(fstype_of "$dev")"
    [[ "$fst" =~ ^(ext4|xfs|btrfs)$ ]] || continue
    size="$(lsblk -bno SIZE "$dev" 2>/dev/null | head -1 || echo 0)"
    size="${size:-0}"
    if [[ "$pk" == "$root_disk" && "$root_disk" == nvme* ]]; then
      # Same NVMe as root: only accept large non-root partitions (≥ 8 GiB).
      (( size >= 8*1024*1024*1024 )) || continue
    fi
    if (( size > best_size )); then
      best_size=$size
      best="$dev"
    fi
  done < <(blkid -o device 2>/dev/null | grep -E '^/dev/nvme[0-9]+n[0-9]+(p[0-9]+)?$' || true)

  if [[ -n "$best" ]]; then
    printf '%s\n' "$best"
    return 0
  fi

  return 1
}

write_state() {
  local mode="$1" dev="${2:-}"
  install -d -m 0755 "$STATE_DIR"
  cat > "$STATE_FILE" <<EOF
RECORD_PATH=${RECORD_PATH}
RECORD_MODE=${mode}
RECORD_DEVICE=${dev}
RECORD_UPDATED=$(date -Is)
EOF
}

mount_recording_volume() {
  local dev="$1"
  reclaim_device "$dev"
  ensure_dir

  if findmnt -n --target "$RECORD_PATH" >/dev/null 2>&1; then
    local cur
    cur="$(findmnt -n -o SOURCE --target "$RECORD_PATH")"
    if [[ "$cur" == "$dev" ]]; then
      log "already mounted: ${dev} → ${RECORD_PATH}"
      ensure_fstab "$dev"
      write_state mounted "$dev"
      return 0
    fi
    # Something else occupies RECORD_PATH — move content aside and remount.
    local bak="${RECORD_PATH}.premount-$(date +%Y%m%d-%H%M%S)"
    warn "${RECORD_PATH} is mounted from ${cur}; replacing with ${dev}"
    umount "$RECORD_PATH" 2>/dev/null || true
  fi

  # If RECORD_PATH is a non-empty directory on the OS disk, stash it so the
  # mount point is clean (old SD-card recordings must not hide under the SSD).
  if [[ -d "$RECORD_PATH" ]] && ! findmnt -n --target "$RECORD_PATH" >/dev/null 2>&1; then
    if find "$RECORD_PATH" -mindepth 1 -maxdepth 1 2>/dev/null | grep -q .; then
      local bak="${RECORD_PATH}.os-backup-$(date +%Y%m%d-%H%M%S)"
      log "moving existing ${RECORD_PATH} → ${bak}"
      mv "$RECORD_PATH" "$bak"
      ensure_dir
    fi
  fi

  ensure_dir
  if ! mount -t "$(fstype_of "$dev" || echo ext4)" "$dev" "$RECORD_PATH"; then
    die "failed to mount ${dev} at ${RECORD_PATH}"
  fi
  log "mounted ${dev} → ${RECORD_PATH}"
  ensure_fstab "$dev"

  # Best-effort: stamp the Kallon label so future boots / blkid -L work.
  if [[ "$(blkid -o value -s LABEL "$dev" 2>/dev/null || true)" != "$RECORD_LABEL" ]]; then
    if command -v e2label >/dev/null 2>&1 && [[ "$(fstype_of "$dev")" == ext4 ]]; then
      e2label "$dev" "$RECORD_LABEL" 2>/dev/null && \
        log "set LABEL=${RECORD_LABEL} on ${dev}" || \
        warn "could not set LABEL=${RECORD_LABEL} on ${dev}"
    fi
  fi

  write_state mounted "$dev"
}

use_root_ssd() {
  ensure_dir
  if path_on_nvme /; then
    log "OS root is NVMe — recordings stay on ${RECORD_PATH} (no separate data volume)"
    write_state root-nvme ""
    return 0
  fi
  warn "no writable NVMe recording volume found — ${RECORD_PATH} is on $(findmnt -n -o SOURCE / || echo unknown)"
  write_state root-fallback ""
  return 0
}

main() {
  require_root
  # Optional env file (systemd EnvironmentFile= or installer load_env).
  if [[ -f /etc/kallon/device.env ]]; then
    # shellcheck disable=SC1091
    set -a; source /etc/kallon/device.env; set +a
  fi
  RECORD_PATH="${RECORD_PATH:-/var/kallon/recordings}"
  RECORD_LABEL="${RECORD_LABEL:-kallon-rec}"

  local dev=""
  if dev="$(find_recording_device)"; then
    mount_recording_volume "$dev"
  else
    use_root_ssd
  fi

  # Final sanity for operators.
  local src size
  src="$(findmnt -n -o SOURCE --target "$RECORD_PATH" 2>/dev/null || echo none)"
  size="$(df -h --output=size,avail,target "$RECORD_PATH" 2>/dev/null | tail -1 || true)"
  log "ready: path=${RECORD_PATH} source=${src} ${size}"
}

main "$@"
