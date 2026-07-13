#!/usr/bin/env python3
"""Export OpenAPI JSON from the enrollment-api app.

Usage (from repo root):
  python scripts/export-openapi.py > openapi.json
  python scripts/export-openapi.py --out sentinel-sdk/docs/static/openapi.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "infra" / "enrollment-api"))

# Minimal env so import works without Postgres.
os.environ.setdefault("KALLON_REGISTRY", "sqlite")
os.environ.setdefault("KALLON_SQLITE_PATH", str(ROOT / ".openapi-export.sqlite"))
os.environ.setdefault("KALLON_PEER_BACKEND", "noop")

from app.main import app  # type: ignore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Kallon Platform API OpenAPI spec")
    parser.add_argument("--out", type=Path, help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

    spec = app.openapi()
    text = json.dumps(spec, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
