#!/usr/bin/env python3
"""Synchronize Atlas images through the isolated browser worker.

The browser process owns source access and throttling. This process owns the
existing image transformation, COS upload, and product Mapping persistence.
It is intentionally a separate batch entrypoint and is not imported by the
Backend or Scheduler request path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import insert as mysql_insert

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.image_sync.browser_bridge_client import BrowserBridgeError, BrowserBridgeImageFetcher
from src.query_governance.models import QueryGovernanceImageAsset
from src.query_governance.relevance import PillowImageTransformer


def build_identity(row: dict[str, str]) -> str:
    return f"atlas|{row['product_id']}|{row['source_url']}"


def build_storage_key(prefix: str, source_url: str) -> str:
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    return f"{prefix.strip('/')}/{digest}.jpg"


def load_rows(path: Path) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            product_id = str(row.get("product_id") or row.get("spu_id") or "").strip()
            source_url = str(row.get("source_url") or "").strip()
            if product_id and source_url:
                unique.setdefault(
                    product_id,
                    {
                        **row,
                        "product_id": product_id,
                        "source_url": source_url,
                        "spu_id": str(row.get("spu_id") or product_id).strip(),
                    },
                )
    return sorted(unique.values(), key=lambda row: row["product_id"])


def load_existing_keys(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    keys: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            key = str(row.get("Key") or row.get("key") or "").strip()
            if key:
                keys.add(key)
    return keys


def load_completed(path: Path, *, execute: bool) -> set[str]:
    if not path.exists():
        return set()
    allowed = {"mapped", "uploaded_existing"} if execute else {"validated", "mapped", "uploaded_existing"}
    completed: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") in allowed and row.get("identity"):
                completed.add(str(row["identity"]))
    return completed


def upsert_mapping(engine, row: dict[str, str], key: str, cached_url: str, processed, image_sha256: str | None) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    values = {
        "brand": "atlas",
        "product_id": row["product_id"],
        "spu_id": row.get("spu_id") or row["product_id"],
        "source_url": row["source_url"],
        "source_url_hash": hashlib.sha256(row["source_url"].encode("utf-8")).hexdigest(),
        "storage_key": key,
        "cached_url": cached_url,
        "status": "ready",
        "content_type": processed.content_type,
        "width": processed.width,
        "height": processed.height,
        "image_sha256": image_sha256,
        "last_success_at": now,
        "last_failure_code": None,
        "last_failure_message": None,
        "created_at": now,
        "updated_at": now,
    }
    statement = mysql_insert(QueryGovernanceImageAsset.__table__).values(values)
    update_columns = (
        "spu_id", "source_url", "source_url_hash", "storage_key", "cached_url", "status",
        "content_type", "width", "height", "image_sha256", "last_success_at",
        "last_failure_code", "last_failure_message", "updated_at",
    )
    with engine.begin() as connection:
        connection.execute(
            statement.on_duplicate_key_update(
                **{column: getattr(statement.inserted, column) for column in update_columns}
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", type=Path, required=True, help="JSONL with product_id and source_url")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "var" / "atlas-browser-sync")
    parser.add_argument("--cos-inventory", type=Path, help="Optional COS inventory JSONL used to reuse objects")
    parser.add_argument("--bridge-url", default=os.getenv("QG_BROWSER_BRIDGE_URL", "http://127.0.0.1:8790"))
    parser.add_argument("--bridge-token", default=os.getenv("QG_BROWSER_BRIDGE_TOKEN"))
    parser.add_argument(
        "--key-prefix",
        default=os.getenv("QUERY_GOVERNANCE_COS_KEY_PREFIX", "query-governance/images/prod/v1"),
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--execute", action="store_true", help="Upload COS and persist production Mapping")
    return parser.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    if args.execute and os.getenv("ENV", "test").strip().lower() != "prod":
        raise SystemExit("--execute requires ENV=prod")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    result_file = args.work_dir / "atlas.results.jsonl"
    rows = load_rows(args.source_file)
    completed = load_completed(result_file, execute=args.execute)
    pending = [row for row in rows if build_identity(row) not in completed]
    if args.limit is not None:
        pending = pending[: args.limit]

    fetcher = BrowserBridgeImageFetcher(args.bridge_url, token=args.bridge_token)
    transformer = PillowImageTransformer(max_edge=768, quality=85, max_output_bytes=1_500_000)
    existing_keys = load_existing_keys(args.cos_inventory)
    engine = cos = None
    bucket = public_base = None
    if args.execute:
        db_uri = os.environ["AUTO_DB_URI_PROD"]
        bucket = os.environ["QUERY_GOVERNANCE_COS_BUCKET"]
        region = os.environ["QUERY_GOVERNANCE_COS_REGION"]
        secret_id = os.environ["TENCENT_COS_SECRET_ID"]
        secret_key = os.environ["TENCENT_COS_SECRET_KEY"]
        engine = create_engine(db_uri, pool_pre_ping=True, connect_args={"connect_timeout": 10})
        from qcloud_cos import CosConfig, CosS3Client

        cos = CosS3Client(CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key))
        public_base = (
            os.getenv("QUERY_GOVERNANCE_COS_PUBLIC_BASE_URL")
            or f"https://{bucket}.cos.{region}.myqcloud.com"
        ).rstrip("/")

    stats = Counter(planned=len(pending))
    for index, row in enumerate(pending, start=1):
        identity = build_identity(row)
        key = build_storage_key(args.key_prefix, row["source_url"])
        result = {"identity": identity, "source_url": row["source_url"], "storage_key": key}
        try:
            if args.execute and key in existing_keys:
                cached_url = f"{public_base}/{quote(key, safe='/')}"
                placeholder = SimpleNamespace(content_type="image/jpeg", width=None, height=None)
                upsert_mapping(engine, row, key, cached_url, placeholder, None)
                stats["uploaded_existing"] += 1
                result["status"] = "uploaded_existing"
            else:
                raw, source_type = fetcher.fetch(row["source_url"])
                processed = transformer.transform(raw, source_type)
                if not args.execute:
                    stats["validated"] += 1
                    result.update(status="validated", bytes=len(processed.payload), width=processed.width, height=processed.height)
                else:
                    cos.put_object(Bucket=bucket, Body=BytesIO(processed.payload), Key=key, ContentType=processed.content_type)
                    cos.head_object(Bucket=bucket, Key=key)
                    cached_url = f"{public_base}/{quote(key, safe='/')}"
                    upsert_mapping(engine, row, key, cached_url, processed, hashlib.sha256(processed.payload).hexdigest())
                    existing_keys.add(key)
                    stats["mapped"] += 1
                    result["status"] = "mapped"
        except Exception as error:  # keep one bad image from stopping the batch
            stats["failed"] += 1
            result.update(status="failed", error=getattr(error, "code", type(error).__name__), message=str(error))
        result["observed_at"] = datetime.now(timezone.utc).isoformat()
        with result_file.open("a", encoding="utf-8") as output:
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
        if index % 10 == 0 or index == len(pending):
            print(f"[atlas-browser] {index}/{len(pending)} stats={dict(stats)}", flush=True)
    print(json.dumps({"mode": "execute" if args.execute else "dry_run", "results": dict(stats)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
