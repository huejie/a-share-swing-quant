"""Verify the newest off-volume backup by restoring it into an isolated temp directory."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile


def verify_latest(root:str|Path)->dict:
    root=Path(root)
    manifests=sorted(root.glob("quant-system-*.manifest.json"),key=lambda item:item.stat().st_mtime,reverse=True)
    if not manifests:raise FileNotFoundError("no backup manifest found")
    manifest_path=manifests[0];manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    database=root/manifest["backup"]
    if not database.is_file():raise FileNotFoundError(database)
    if sha256(database.read_bytes()).hexdigest()!=manifest["sha256"]:raise ValueError("database checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="a-share-quant-restore-") as temp:
        restored=Path(temp)/database.name;shutil.copy2(database,restored)
        connection=sqlite3.connect(f"file:{restored}?mode=ro",uri=True)
        try:integrity=connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:connection.close()
        if integrity!="ok":raise ValueError(f"restored database integrity failed: {integrity}")
        for entry in manifest.get("artifacts",[]):
            archive=root/entry["archive"]
            if sha256(archive.read_bytes()).hexdigest()!=entry["sha256"]:raise ValueError("artifact checksum mismatch")
            with tarfile.open(archive,"r:gz") as bundle:
                target=Path(temp)/"artifacts"
                for member in bundle.getmembers():
                    resolved=(target/member.name).resolve()
                    if target.resolve() not in resolved.parents and resolved!=target.resolve():
                        raise ValueError("unsafe artifact archive path")
                bundle.extractall(target,filter="data")
    return {"status":"verified","manifest":manifest_path.name,"database":database.name,
            "artifacts":len(manifest.get("artifacts",[])),"integrity_check":"ok"}


def main()->None:
    parser=argparse.ArgumentParser();parser.add_argument("--input",required=True)
    args=parser.parse_args();print(json.dumps(verify_latest(args.input),ensure_ascii=False))


if __name__=="__main__":main()
