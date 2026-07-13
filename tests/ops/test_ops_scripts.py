from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def run_script(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / name), *args],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def test_operational_scripts_have_strict_safety_markers():
    required = {"dev.ps1", "test.ps1", "eod.ps1", "backup.ps1", "restore-verify.ps1"}
    assert required <= {path.name for path in (ROOT / "scripts").glob("*.ps1")}
    restore = (ROOT / "scripts" / "restore-verify.ps1").read_text(encoding="utf-8")
    backup = (ROOT / "scripts" / "backup.ps1").read_text(encoding="utf-8")
    dev = (ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    assert "Parameter(Mandatory=$true)" in restore
    assert "尚不存在的新目录" in restore
    assert "Get-FileHash" in restore and "integrity_check" in restore
    helper = (ROOT / "scripts" / "sqlite_tools.py").read_text(encoding="utf-8")
    assert "source_database.backup(target_database)" in helper and "Get-FileHash" in backup
    assert "-WindowStyle Hidden" in dev
    combined = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "scripts").glob("*.ps1"))
    assert "券商" not in combined or "不要" in combined or "不连接" in combined


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is required for Windows delivery scripts")
def test_backup_restore_is_copy_only_and_hash_verified(tmp_path: Path):
    source = tmp_path / "live.db"
    artifact = tmp_path / "research"
    artifact.mkdir()
    (artifact / "gate.json").write_text('{"status":"engineering_candidate"}', encoding="utf-8")
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE audit(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("INSERT INTO audit(payload) VALUES ('immutable decision')")

    backups = tmp_path / "backups"
    result = run_script(
        "backup.ps1",
        "-DatabasePath", str(source),
        "-DestinationRoot", str(backups),
        "-ArtifactPaths", str(artifact),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    backup_dirs = list(backups.glob("backup-*"))
    assert len(backup_dirs) == 1
    manifest = json.loads((backup_dirs[0] / "manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["sqlite_integrity"] == "ok" and manifest["automatic_trading"] is False
    assert any(item["path"] == "database/quant_system.db" for item in manifest["files"])

    restored = tmp_path / "restored-new"
    verify = run_script("restore-verify.ps1", "-BackupPath", str(backup_dirs[0]), "-RestoreDirectory", str(restored))
    assert verify.returncode == 0, verify.stderr + verify.stdout
    with sqlite3.connect(restored / "database" / "quant_system.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT payload FROM audit").fetchone()[0] == "immutable decision"
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT payload FROM audit").fetchone()[0] == "immutable decision"

    repeat = run_script("restore-verify.ps1", "-BackupPath", str(backup_dirs[0]), "-RestoreDirectory", str(restored))
    assert repeat.returncode != 0
    assert "尚不存在" in (repeat.stderr + repeat.stdout)


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is required for Windows delivery scripts")
def test_restore_rejects_tampered_backup(tmp_path: Path):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE t(value TEXT)")
        db.execute("INSERT INTO t VALUES ('safe')")
    backups = tmp_path / "backups"
    result = run_script("backup.ps1", "-DatabasePath", str(source), "-DestinationRoot", str(backups), "-ArtifactPaths", str(tmp_path / "missing"))
    assert result.returncode == 0, result.stderr + result.stdout
    backup = next(backups.glob("backup-*"))
    with (backup / "database" / "quant_system.db").open("ab") as stream:
        stream.write(b"tampered")
    restored = tmp_path / "must-not-exist"
    verify = run_script("restore-verify.ps1", "-BackupPath", str(backup), "-RestoreDirectory", str(restored))
    assert verify.returncode != 0
    assert not restored.exists()
