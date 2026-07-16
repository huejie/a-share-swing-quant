"""Point-in-time records and immutable raw-batch verification.

Visibility is based on the latest of published_at and available_at, never on
the economic period alone.  The module stores evidence; it does not infer a
commercial data licence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_time(value: str | datetime | date, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time(23, 59, 59) if end_of_day else time(), SHANGHAI)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def file_sha256(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class RawBatchManifest:
    batch_id: str
    provider: str
    created_at: str
    files: Mapping[str, str]
    metadata_hash: str
    manifest_hash: str

    @classmethod
    def create(cls, root: str | Path, *, batch_id: str, provider: str,
               created_at: datetime, files: Iterable[str], metadata: Mapping[str, Any]) -> "RawBatchManifest":
        root = Path(root)
        hashes = {name: file_sha256(root / name) for name in sorted(set(files))}
        body = {"batch_id": batch_id, "provider": provider, "created_at": created_at.isoformat(),
                "files": hashes, "metadata_hash": sha256(canonical_json(metadata).encode()).hexdigest()}
        return cls(**body, manifest_hash=sha256(canonical_json(body).encode()).hexdigest())

    def verify(self, root: str | Path, metadata: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
        root = Path(root); errors = []
        for name, expected in self.files.items():
            path = root / name
            if not path.is_file(): errors.append(f"missing:{name}")
            elif file_sha256(path) != expected: errors.append(f"tampered:{name}")
        if sha256(canonical_json(metadata).encode()).hexdigest() != self.metadata_hash:
            errors.append("metadata_hash_mismatch")
        body = {k: v for k, v in asdict(self).items() if k != "manifest_hash"}
        if sha256(canonical_json(body).encode()).hexdigest() != self.manifest_hash:
            errors.append("manifest_hash_mismatch")
        return not errors, tuple(errors)


@dataclass(frozen=True)
class PITRecord:
    dataset: str
    entity_id: str
    effective_at: datetime
    published_at: datetime
    available_at: datetime
    payload: Mapping[str, Any]
    revision: int = 1
    source_ref: str = ""
    collected_at: datetime | None = None
    parser_version: str = ""

    def __post_init__(self):
        for name in ("effective_at", "published_at", "available_at"):
            object.__setattr__(self, name, parse_time(getattr(self, name)))
        if self.collected_at is not None:
            object.__setattr__(self, "collected_at", parse_time(self.collected_at))
        if self.revision < 1: raise ValueError("revision must be positive")

    @property
    def visible_at(self) -> datetime:
        return max(self.published_at, self.available_at)

    def visible_as_of(self, as_of: datetime | date) -> bool:
        cutoff = parse_time(as_of, end_of_day=isinstance(as_of, date) and not isinstance(as_of, datetime))
        return self.effective_at <= cutoff and self.visible_at <= cutoff


@dataclass(frozen=True)
class SecurityHistory:
    symbol: str
    name: str
    listed_at: date
    delisted_at: date | None = None
    board: str = "主板"

    def active_as_of(self, as_of: date) -> bool:
        return self.listed_at <= as_of and (self.delisted_at is None or as_of < self.delisted_at)


@dataclass(frozen=True)
class ThemeMembership:
    symbol: str
    theme: str
    effective_from: date
    effective_to: date | None
    published_at: datetime
    available_at: datetime

    def visible_as_of(self, as_of: datetime | date) -> bool:
        cutoff = parse_time(as_of, end_of_day=isinstance(as_of, date) and not isinstance(as_of, datetime))
        day = cutoff.astimezone(SHANGHAI).date()
        return (self.effective_from <= day and (self.effective_to is None or day < self.effective_to)
                and max(parse_time(self.published_at), parse_time(self.available_at)) <= cutoff)


@dataclass
class PointInTimeStore:
    records: list[PITRecord] = field(default_factory=list)
    securities: list[SecurityHistory] = field(default_factory=list)
    memberships: list[ThemeMembership] = field(default_factory=list)

    def append(self, record: PITRecord) -> None:
        identity = (record.dataset, record.entity_id, record.effective_at, record.revision)
        if any((r.dataset, r.entity_id, r.effective_at, r.revision) == identity for r in self.records):
            raise ValueError(f"duplicate immutable PIT record: {identity}")
        self.records.append(record)

    def records_as_of(self, as_of: datetime | date, dataset: str | None = None) -> tuple[PITRecord, ...]:
        visible = [r for r in self.records if r.visible_as_of(as_of) and (dataset is None or r.dataset == dataset)]
        latest: dict[tuple[str, str, datetime], PITRecord] = {}
        for record in visible:
            key = (record.dataset, record.entity_id, record.effective_at)
            if key not in latest or record.revision > latest[key].revision: latest[key] = record
        return tuple(sorted(latest.values(), key=lambda r: (r.dataset, r.entity_id, r.effective_at)))

    def universe_as_of(self, as_of: date) -> tuple[SecurityHistory, ...]:
        return tuple(sorted((s for s in self.securities if s.active_as_of(as_of)), key=lambda s: s.symbol))

    def theme_as_of(self, symbol: str, as_of: datetime | date) -> str | None:
        candidates = [m for m in self.memberships if m.symbol == symbol and m.visible_as_of(as_of)]
        return max(candidates, key=lambda m: m.effective_from).theme if candidates else None

    def reconstruct(self, as_of: datetime | date) -> dict[str, Any]:
        cutoff = parse_time(as_of, end_of_day=isinstance(as_of, date) and not isinstance(as_of, datetime))
        return {"as_of": cutoff.isoformat(), "securities": [asdict(x) for x in self.universe_as_of(cutoff.date())],
                "themes": {s.symbol: self.theme_as_of(s.symbol, cutoff) for s in self.universe_as_of(cutoff.date())},
                "records": [asdict(x) for x in self.records_as_of(cutoff)]}
