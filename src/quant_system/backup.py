"""Consistent SQLite backup with a checksum manifest and bounded retention."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


def create_backup(source: str | Path, output: str | Path, retention_days: int = 30) -> dict:
    source_path = Path(source)
    output_path = Path(output)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source_path}")
    if retention_days < 7:
        raise ValueError("retention_days must be at least 7")
    output_path.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    database_name = f"quant-system-{stamp}.sqlite3"
    database_path = output_path / database_name
    with sqlite3.connect(source_path) as source_db, sqlite3.connect(database_path) as backup_db:
        source_db.backup(backup_db)
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        database_path.unlink(missing_ok=True)
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    digest = sha256(database_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "sqlite-backup/v1", "created_at": generated_at.isoformat(),
        "source_name": source_path.name, "backup": database_name,
        "sha256": digest, "size_bytes": database_path.stat().st_size,
        "integrity_check": integrity, "retention_days": retention_days,
    }
    manifest_path = output_path / f"quant-system-{stamp}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    cutoff = generated_at - timedelta(days=retention_days)
    for candidate in output_path.glob("quant-system-*"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
        if modified < cutoff and candidate not in {database_path, manifest_path}:
            candidate.unlink()
    return {**manifest, "manifest": manifest_path.name}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--retention-days", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(create_backup(args.source, args.output, args.retention_days), ensure_ascii=False))


if __name__ == "__main__":
    main()
