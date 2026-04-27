#!/usr/bin/env python3
"""Import a local Strata library into the configured Azure backend.

Prereqs:
  1. Run azure_schema.sql in Azure Database for PostgreSQL.
  2. Export the Azure variables from .env.example.
  3. Create/sign in once so profiles has a firm_id, or pass --firm-id and
     --user-id values from Microsoft Entra.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from azure_store import AuthContext, store  # noqa: E402
from sorter import _BUCKET_FALLBACK, _CLIENT_FALLBACK, sanitize_bucket, sanitize_client, sanitize_snake  # noqa: E402


def read_json(path: Path, fallback):
    if not path.is_file():
        return fallback
    return json.loads(path.read_text())


def iter_jsonl(path: Path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--firm-id", required=True, help="Target firm UUID")
    parser.add_argument("--user-id", required=True, help="Microsoft Entra object id to attribute imported rows to")
    parser.add_argument("--email", default="", help="Optional user email for event attribution")
    parser.add_argument("--root", default=str(ROOT), help="Repo root containing sorted/.manifest.json")
    args = parser.parse_args()

    if not store.enabled:
        raise SystemExit("Azure env vars are not configured")

    root = Path(args.root)
    sorted_dir = root / "sorted"
    manifest = read_json(sorted_dir / ".manifest.json", {"files": [], "clients": [], "client_taxonomies": {}})
    ctx = AuthContext(user_id=args.user_id, email=args.email, firm_id=args.firm_id, role="owner")

    for client in manifest.get("clients", []):
        client_name = sanitize_client(client)
        if client_name != _CLIENT_FALLBACK:
            client_row = store.ensure_client(ctx.firm_id, client_name)
            for bucket in manifest.get("client_taxonomies", {}).get(client_name, []):
                bucket_name = sanitize_bucket(bucket)
                if bucket_name != _BUCKET_FALLBACK:
                    store.ensure_bucket(ctx.firm_id, client_row["id"] if client_row else None, bucket_name)

    imported = 0
    echoed = 0
    skipped_existing = 0
    for entry in manifest.get("files", []):
        final_name = entry.get("final_name")
        if not final_name:
            continue
        src = sorted_dir / final_name
        if not src.is_file():
            print(f"missing file, skipping: {final_name}")
            continue
        file_hash = entry.get("hash") or hashlib.sha256(src.read_bytes()).hexdigest()
        if store.find_document_by_hash(ctx.firm_id, file_hash):
            skipped_existing += 1
            continue
        original = entry.get("original") or final_name
        ext = Path(final_name).suffix or Path(original).suffix or ".bin"
        stem = sanitize_snake(Path(final_name).stem, original)
        client_name = sanitize_client(entry.get("client") or "")
        bucket_name = sanitize_bucket(entry.get("bucket") or "")
        doc = store.create_document(
            ctx,
            src=src,
            original=original,
            stem=stem,
            ext=ext,
            file_hash=file_hash,
            client_name=client_name,
            bucket_name=bucket_name,
            gemma_bucket=entry.get("gemma_bucket") or entry.get("gemma_predicted_bucket"),
            gemma_suggested=entry.get("gemma_suggested"),
        )
        imported += 1
        for echo in entry.get("echoes", []) or []:
            store.add_echo(ctx, doc["id"], echo.get("original") or original)
            echoed += 1

    event_count = 0
    for path in (sorted_dir / ".analyst_events.jsonl", sorted_dir / ".bucket_changes.jsonl"):
        for event in iter_jsonl(path) or []:
            store.log_event(ctx, event)
            event_count += 1

    print(json.dumps({
        "imported_documents": imported,
        "skipped_existing_documents": skipped_existing,
        "imported_echoes": echoed,
        "imported_analyst_events": event_count,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
