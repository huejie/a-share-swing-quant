"""Small stdlib-only helper used by PowerShell delivery scripts."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3


def integrity(path: Path) -> None:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        row = database.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise SystemExit(f"integrity_check failed: {row}")


def backup(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_database, sqlite3.connect(target) as target_database:
        source_database.backup(target_database)
    integrity(target)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("source", type=Path)
    backup_parser.add_argument("target", type=Path)
    integrity_parser = subparsers.add_parser("integrity")
    integrity_parser.add_argument("database", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "backup":
        backup(arguments.source, arguments.target)
    else:
        integrity(arguments.database)
    print("ok")


if __name__ == "__main__":
    main()
