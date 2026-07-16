import json
from pathlib import Path
import sqlite3

import pytest

from quant_system.backup import create_backup
from quant_system.backup_verify import verify_latest


def test_backup_includes_research_artifacts_and_is_restore_verified(tmp_path:Path):
    source=tmp_path/"source.db"
    connection=sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE proof(value TEXT)")
        connection.execute("INSERT INTO proof VALUES('ok')")
        connection.commit()
    finally:connection.close()
    research=tmp_path/"research";research.mkdir();(research/"gates.json").write_text('{"overall":"FAIL"}',encoding="utf-8")
    output=tmp_path/"backups"

    manifest=create_backup(source,output,30,[research])
    result=verify_latest(output)

    assert manifest["artifacts"] and result["status"]=="verified" and result["artifacts"]==1


def test_restore_verifier_rejects_tampered_database(tmp_path:Path):
    source=tmp_path/"source.db"
    connection=sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE proof(value TEXT)");connection.commit()
    finally:connection.close()
    output=tmp_path/"backups";manifest=create_backup(source,output)
    with (output/manifest["backup"]).open("ab") as handle:handle.write(b"tampered")
    with pytest.raises(ValueError,match="checksum"):
        verify_latest(output)
