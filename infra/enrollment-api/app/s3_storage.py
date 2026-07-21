"""S3-compatible storage helpers (AWS S3, Backblaze B2) for recording playback."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("s3_storage")

_client = None


def _bucket() -> str:
    return os.environ.get("KALLON_S3_BUCKET", "").strip()


def _region() -> str:
    return os.environ.get("KALLON_S3_REGION", "us-east-005").strip()


def _endpoint() -> Optional[str]:
    raw = os.environ.get("KALLON_S3_ENDPOINT", os.environ.get("S3_ENDPOINT", "")).strip()
    return raw or None


def _presign_ttl() -> int:
    raw = os.environ.get("KALLON_S3_PRESIGN_TTL_SEC", "3600").strip()
    try:
        return max(60, min(int(raw), 86400))
    except ValueError:
        return 3600


def configured() -> bool:
    return bool(_bucket() and os.environ.get("AWS_ACCESS_KEY_ID"))


def _client_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "region_name": _region(),
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "").strip(),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip(),
    }
    endpoint = _endpoint()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return kwargs


def _client():
    global _client
    if _client is not None:
        return _client
    if not configured():
        raise RuntimeError(
            "S3/B2 not configured — set KALLON_S3_BUCKET, KALLON_S3_ENDPOINT, and AWS credentials"
        )
    import boto3

    _client = boto3.client(**_client_kwargs())
    return _client


def presign_get_object(
    *,
    bucket: str,
    key: str,
    download: bool = False,
    filename: Optional[str] = None,
) -> dict[str, int | str]:
    params: dict[str, str] = {"Bucket": bucket, "Key": key}
    if download and filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    ttl = _presign_ttl()
    url = _client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=ttl,
    )
    return {"url": url, "expires_in": ttl}


def delete_object(*, bucket: str, key: str) -> None:
    _client().delete_object(Bucket=bucket, Key=key)


def head_object(*, bucket: str, key: str) -> dict:
    return _client().head_object(Bucket=bucket, Key=key)
