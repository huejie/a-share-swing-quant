"""Consistent SQLite backup with a checksum manifest and bounded retention."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tarfile


def create_backup(source: str | Path, output: str | Path, retention_days: int = 30,
                  artifacts: list[str | Path] | None = None) -> dict:
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
    artifact_entries=[]
    existing=[Path(item) for item in (artifacts or []) if Path(item).exists()]
    if existing:
        archive_name=f"quant-system-{stamp}-artifacts.tar.gz"
        archive_path=output_path/archive_name
        with tarfile.open(archive_path,"w:gz") as archive:
            for item in existing:
                archive.add(item,arcname=item.name,recursive=True)
        artifact_entries.append({"archive":archive_name,"sha256":sha256(archive_path.read_bytes()).hexdigest(),
                                 "size_bytes":archive_path.stat().st_size,
                                 "sources":[str(item) for item in existing]})
    manifest = {
        "schema_version": "sqlite-backup/v1", "created_at": generated_at.isoformat(),
        "source_name": source_path.name, "backup": database_name,
        "sha256": digest, "size_bytes": database_path.stat().st_size,
        "integrity_check": integrity, "retention_days": retention_days,
        "artifacts":artifact_entries,
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
    parser.add_argument("--artifact", action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(create_backup(args.source, args.output, args.retention_days,args.artifact), ensure_ascii=False))


if __name__ == "__main__":
    main()
