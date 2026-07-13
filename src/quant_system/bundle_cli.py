"""Build and validate hash-locked local licensed CSV bundles."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

from .pit import RawBatchManifest
from .providers import LicensedCsvBundleProvider

REQUIRED = ("bars.csv", "securities.csv", "theme_memberships.csv", "metadata.json")


def _read_declared_metadata(root: Path) -> dict:
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing: raise ValueError(f"missing required bundle files: {', '.join(missing)}")
    metadata = json.loads((root / "metadata.json").read_text("utf-8"))
    authorization = metadata.get("authorization", {})
    pit = metadata.get("pit", {})
    if authorization.get("authorized") is not True or not authorization.get("scope"):
        raise ValueError("metadata must already declare authorization.authorized=true and a non-empty scope; CLI will not invent authorization")
    if pit.get("verified") is not True or not pit.get("method"):
        raise ValueError("metadata must already declare pit.verified=true and a non-empty verification method")
    if not metadata.get("batch_id"): raise ValueError("metadata.batch_id is required")
    if not isinstance(metadata.get("datasets"), dict) or not metadata["datasets"]:
        raise ValueError("metadata.datasets with per-dataset as_of/freshness declarations is required")
    invalid_datasets = [name for name, value in metadata["datasets"].items()
                        if not isinstance(value, dict) or not value.get("as_of") or "required" not in value]
    if invalid_datasets: raise ValueError(f"dataset metadata requires as_of and required: {', '.join(invalid_datasets)}")
    return {k: v for k, v in metadata.items() if k != "manifest"}


def build_bundle(input_dir: str | Path, output_dir: str | Path | None = None) -> dict:
    source = Path(input_dir).resolve(); target = Path(output_dir).resolve() if output_dir else source
    declared = _read_declared_metadata(source)
    files = [name for name in ("bars.csv", "securities.csv", "theme_memberships.csv", "pit_records.csv") if (source / name).is_file()]
    target.mkdir(parents=True, exist_ok=True)
    if target != source:
        for name in files: shutil.copy2(source / name, target / name)
    manifest = RawBatchManifest.create(target, batch_id=str(declared["batch_id"]),
                                       provider=str(declared.get("provider", "licensed-csv-bundle")),
                                       created_at=datetime.now(ZoneInfo("Asia/Shanghai")), files=files, metadata=declared)
    (target / "metadata.json").write_text(json.dumps({**declared, "manifest": asdict(manifest)}, ensure_ascii=False, indent=2), "utf-8")
    status = LicensedCsvBundleProvider(target).status()
    if not status["production_ready"]: raise RuntimeError(f"built bundle failed validation: {status}")
    return {"bundle": str(target), "batch_id": declared["batch_id"], "manifest_hash": manifest.manifest_hash,
            "files": list(manifest.files), "production_ready": True}


def validate_bundle(bundle: str | Path) -> dict:
    provider = LicensedCsvBundleProvider(bundle); status = provider.status()
    if not status["production_ready"]: raise ValueError(status["reason"] + f"; errors={status['manifest_errors']}")
    return {**status, "bundle": str(Path(bundle).resolve())}


def _run(action: str) -> None:
    parser = argparse.ArgumentParser(description="构建或校验本地授权/PIT CSV 数据包")
    if action == "build":
        parser.add_argument("--input", required=True); parser.add_argument("--output")
    else: parser.add_argument("--bundle", required=True)
    args = parser.parse_args()
    try: result = build_bundle(args.input, args.output) if action == "build" else validate_bundle(args.bundle)
    except Exception as exc:
        print(json.dumps({"status":"FAIL","error":str(exc)},ensure_ascii=False),file=sys.stderr); raise SystemExit(2)
    print(json.dumps({"status":"PASS",**result},ensure_ascii=False,indent=2))


def build_main() -> None: _run("build")
def validate_main() -> None: _run("validate")
