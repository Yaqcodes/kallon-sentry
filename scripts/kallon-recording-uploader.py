#!/usr/bin/env python3
"""Tower recording upload worker — S3 upload before local delete.

Watches RECORD_PATH/camN/*.mp4 for closed MediaMTX segments, uploads to S3,
registers metadata with the Platform API, then deletes local copies only after
HeadObject verification (+ optional grace period).

Configure via /etc/kallon/device.env (see deploy/device.env.example).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("kallon-recording-uploader")

FMP4_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:-\d+)?\.mp4$", re.I)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_duration_sec(raw: str, *, default: int) -> int:
    """Parse Go-style duration (15m, 1h, 90s) or bare integer seconds."""
    text = (raw or "").strip().lower()
    if not text:
        return default
    if text.isdigit():
        return max(1, int(text))
    m = re.fullmatch(r"(\d+)(ms|s|m|h)", text)
    if not m:
        return default
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "ms":
        return max(1, n // 1000)
    if unit == "s":
        return max(1, n)
    if unit == "m":
        return max(1, n * 60)
    return max(1, n * 3600)


def load_device_env(path: str = "/etc/kallon/device.env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def record_path() -> Path:
    return Path(os.environ.get("RECORD_PATH", "/var/kallon/recordings"))


def manifest_path() -> Path:
    return record_path() / ".upload-manifest.json"


def state_path() -> Path:
    return record_path() / ".upload-state.json"


def platform_base() -> str:
    explicit = os.environ.get("RECORD_UPLOAD_PLATFORM_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    enroll = os.environ.get("ENROLLMENT_URL", "").strip()
    if enroll:
        return enroll.split("/v1")[0].rstrip("/")
    return ""


def ingest_token() -> str:
    return (
        os.environ.get("RECORD_UPLOAD_INGEST_TOKEN", "").strip()
        or os.environ.get("KALLON_RECORDING_INGEST_TOKEN", "").strip()
    )


def s3_prefix() -> str:
    raw = os.environ.get("S3_PREFIX", "").strip().strip("/")
    return raw


def parse_segment_start(name: str) -> Optional[datetime]:
    m = FMP4_RE.match(name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_manifest() -> dict[str, Any]:
    path = manifest_path()
    if not path.is_file():
        return {"uploaded": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("uploaded"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"uploaded": {}}


def save_manifest(data: dict[str, Any]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_state(**fields: Any) -> None:
    path = state_path()
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(fields)
    existing["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_camera_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("cam*") if p.is_dir())


def segment_key(camera_dir: Path, filename: str) -> str:
    return f"{camera_dir.name}/{filename}"


def camera_num(camera_dir: Path) -> int:
    name = camera_dir.name
    if name.startswith("cam") and name[3:].isdigit():
        return int(name[3:])
    return 1


def s3_object_key(device_id: str, camera: int, filename: str) -> str:
    """B2 layout: {device_id}/cam{N}/{filename} (optional S3_PREFIX prefix)."""
    prefix = s3_prefix()
    parts = [p for p in (prefix, device_id, f"cam{camera}", filename) if p]
    return "/".join(parts)


def _s3_endpoint() -> str | None:
    raw = os.environ.get("S3_ENDPOINT", "").strip()
    return raw or None


def make_s3_client():
    import boto3

    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "region_name": os.environ.get("S3_REGION", "us-east-005").strip(),
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "").strip(),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip(),
    }
    endpoint = _s3_endpoint()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client(**kwargs)


def discover_pending(
    root: Path,
    manifest: dict[str, Any],
    *,
    stable_sec: int,
) -> list[tuple[Path, str, int]]:
    uploaded = manifest.get("uploaded", {})
    now = time.time()
    pending: list[tuple[Path, str, int]] = []
    for cam_dir in list_camera_dirs(root):
        files = sorted(cam_dir.glob("*.mp4"), key=lambda p: p.name)
        for idx, path in enumerate(files):
            rel = segment_key(cam_dir, path.name)
            if rel in uploaded:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if now - st.st_mtime < stable_sec:
                continue
            # Newest file may still be open — skip unless older than 2× stable window.
            if idx == len(files) - 1 and now - st.st_mtime < stable_sec * 2:
                continue
            pending.append((path, rel, camera_num(cam_dir)))
    pending.sort(key=lambda t: t[0].stat().st_mtime)
    return pending


def remux_progressive(src: Path) -> Path:
    """Remux MediaMTX fMP4 → progressive MP4 (moov at start) for browser/VLC playback.

    Returns a temp path the caller must delete. Stream-copy only (no re-encode).
    """
    import tempfile

    fd, name = tempfile.mkstemp(prefix="kallon-remux-", suffix=".mp4")
    os.close(fd)
    dst = Path(name)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-c", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    except FileNotFoundError as exc:
        dst.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg not installed — required to remux recordings for playback") from exc
    except subprocess.TimeoutExpired as exc:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg remux timed out for {src.name}") from exc
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size < 1:
        err = (proc.stderr or proc.stdout or "ffmpeg failed").strip()[-500:]
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg remux failed for {src.name}: {err}")
    return dst


def upload_and_verify(
    client,
    bucket: str,
    key: str,
    path: Path,
    digest: Optional[str],
    *,
    force: bool = False,
) -> None:
    local_size = path.stat().st_size
    if not force:
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            remote_size = int(head.get("ContentLength") or 0)
            if remote_size == local_size:
                log.info("s3 object already present %s (%s bytes) — skip put", key, local_size)
                return
        except Exception:
            pass

    extra: dict[str, Any] = {
        "ContentType": "video/mp4",
        "ServerSideEncryption": "AES256",
    }
    if digest:
        extra["Metadata"] = {"sha256": digest}
    client.upload_file(str(path), bucket, key, ExtraArgs=extra)
    head = client.head_object(Bucket=bucket, Key=key)
    remote_size = int(head.get("ContentLength") or 0)
    if remote_size != local_size:
        raise RuntimeError(f"S3 size mismatch for {key}: local={local_size} remote={remote_size}")


def post_ingest(payload: dict[str, Any]) -> None:
    base = platform_base()
    if not base:
        raise RuntimeError("RECORD_UPLOAD_PLATFORM_URL / ENROLLMENT_URL not configured")
    url = f"{base}/v1/recordings/ingest"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = ingest_token()
    if token:
        headers["X-Kallon-Ingest-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"ingest HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ingest HTTP {exc.code}: {detail}") from exc


def maybe_delete_local(path: Path, manifest_entry: dict[str, Any]) -> None:
    # Default OFF: local lifetime is owned by MediaMTX recordDeleteAfter
    # (RECORD_MEDIAMTX_DELETE_AFTER). Early delete after upload would break
    # "keep until retention elapses even after cloud upload."
    if not _env_bool("RECORD_LOCAL_DELETE_AFTER_UPLOAD", False):
        return
    grace_min = _env_int("RECORD_LOCAL_GRACE_MIN", 15)
    uploaded_at = manifest_entry.get("uploaded_at")
    if uploaded_at:
        try:
            dt = datetime.fromisoformat(str(uploaded_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            if age_min < grace_min:
                return
        except ValueError:
            pass
    try:
        path.unlink()
        log.info("deleted local segment %s", path)
    except OSError as exc:
        log.warning("failed to delete local %s: %s", path, exc)


def prune_uploaded_locals(root: Path, manifest: dict[str, Any]) -> None:
    uploaded = manifest.get("uploaded", {})
    for rel, entry in list(uploaded.items()):
        local = root / rel.replace("/", os.sep)
        if local.is_file():
            maybe_delete_local(local, entry)


def process_once() -> int:
    if not _env_bool("RECORD_UPLOAD_ENABLE", False):
        write_state(enabled=False, pending=0, note="RECORD_UPLOAD_ENABLE=0")
        return 0

    bucket = os.environ.get("S3_BUCKET", "").strip()
    customer_id = os.environ.get("CUSTOMER_ID", "").strip()
    device_id = os.environ.get("DEVICE_ID", "").strip()
    if not bucket or not customer_id or not device_id:
        write_state(enabled=True, pending=0, last_error="missing S3_BUCKET/CUSTOMER_ID/DEVICE_ID")
        return 0
    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        write_state(enabled=True, pending=0, last_error="missing AWS credentials")
        return 0

    root = record_path()
    if not root.is_dir():
        write_state(enabled=True, pending=0, last_error=f"{root} missing")
        return 0

    stable_sec = _env_int("RECORD_UPLOAD_STABLE_SEC", 120)
    concurrency = max(1, _env_int("RECORD_UPLOAD_CONCURRENCY", 2))
    manifest = load_manifest()
    pending = discover_pending(root, manifest, stable_sec=stable_sec)
    write_state(enabled=True, pending=len(pending), uploading=0, last_error=None)

    if not pending:
        prune_uploaded_locals(root, manifest)
        return 0

    client = make_s3_client()
    uploaded_count = 0
    # Segment length for ingest metadata — always from device.env (via process env).
    # Accept Go-style durations (15m, 1h) or bare seconds.
    raw_seg = (
        os.environ.get("RECORD_MEDIAMTX_SEGMENT_FILE_DURATION")
        or os.environ.get("RECORD_SEGMENT_DURATION")
        or "15m"
    ).strip()
    segment_duration_sec = _parse_duration_sec(raw_seg, default=15 * 60)

    # Full-file SHA-256 of ~15m / 400MB+ segments dominates CPU wall time.
    # Default off; size+HeadObject verify is enough for the upload-before-delete gate.
    compute_sha = _env_bool("RECORD_UPLOAD_SHA256", False)

    for path, rel, camera in pending[:concurrency]:
        write_state(uploading=1)
        remuxed: Optional[Path] = None
        try:
            # MediaMTX writes fragmented MP4; browsers/VLC need progressive (faststart).
            remuxed = remux_progressive(path)
            upload_path = remuxed
            digest = sha256_file(upload_path) if compute_sha else None
            filename = path.name
            key = s3_object_key(device_id, camera, filename)
            # Always put remuxed bytes (size differs from raw fMP4 — do not skip on old size).
            upload_and_verify(client, bucket, key, upload_path, digest, force=True)
            # Remuxed size differs from raw fMP4; force put so old fMP4 objects are replaced.
            started = parse_segment_start(filename) or datetime.fromtimestamp(                path.stat().st_mtime, tz=timezone.utc
            )
            ended = started + timedelta(seconds=segment_duration_sec)
            upload_size = upload_path.stat().st_size
            ingest = {
                "device_id": device_id,
                "camera": camera,
                "filename": filename,
                "s3_bucket": bucket,
                "s3_key": key,
                "size_bytes": upload_size,
                "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ended_at": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_sec": segment_duration_sec,
            }
            if digest:
                ingest["sha256_hex"] = digest
            post_ingest(ingest)
            manifest.setdefault("uploaded", {})[rel] = {
                "s3_key": key,
                "sha256_hex": digest,
                "size_bytes": upload_size,
                "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "remuxed": True,
            }
            save_manifest(manifest)
            maybe_delete_local(path, manifest["uploaded"][rel])
            uploaded_count += 1
            write_state(
                uploaded_total=len(manifest.get("uploaded", {})),
                last_upload_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                last_error=None,
            )
            log.info("uploaded %s → s3://%s/%s (remuxed %s bytes)", rel, bucket, key, upload_size)
        except Exception as exc:  # noqa: BLE001
            log.exception("upload failed for %s", rel)
            write_state(last_error=str(exc))
            break
        finally:
            if remuxed is not None:
                try:
                    remuxed.unlink(missing_ok=True)
                except OSError:
                    pass

    prune_uploaded_locals(root, manifest)
    remaining = discover_pending(root, manifest, stable_sec=stable_sec)
    write_state(pending=len(remaining), uploading=0)
    return uploaded_count


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_device_env()
    if "--once" in argv:
        process_once()
        return 0
    poll_sec = _env_int("RECORD_UPLOAD_POLL_SEC", 60)
    log.info("recording uploader started (poll=%ss)", poll_sec)
    while True:
        try:
            process_once()
        except Exception:  # noqa: BLE001
            log.exception("upload loop error")
        time.sleep(poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())
