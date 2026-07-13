from dataclasses import asdict
from datetime import date, datetime
import csv
import json
from zoneinfo import ZoneInfo

from quant_system.models import DataSnapshot
from quant_system.pit import PITRecord, PointInTimeStore, RawBatchManifest, SecurityHistory, ThemeMembership
from quant_system.providers import LicensedCsvBundleProvider
from quant_system.quality import check_quality

SH = ZoneInfo("Asia/Shanghai")


def test_future_financial_and_announcement_records_never_leak():
    store = PointInTimeStore()
    store.append(PITRecord("financials", "600001:2024FY", datetime(2024,12,31,tzinfo=SH),
                           datetime(2025,4,1,18,tzinfo=SH), datetime(2025,4,1,18,5,tzinfo=SH), {"profit":100}))
    store.append(PITRecord("announcements", "A-001", datetime(2025,3,1,tzinfo=SH),
                           datetime(2025,3,2,20,tzinfo=SH), datetime(2025,3,2,20,3,tzinfo=SH), {"title":"重大合同"}))
    assert store.records_as_of(datetime(2025,3,2,19,tzinfo=SH)) == ()
    assert [r.dataset for r in store.records_as_of(datetime(2025,3,3,tzinfo=SH))] == ["announcements"]
    assert {r.dataset for r in store.records_as_of(datetime(2025,4,2,tzinfo=SH))} == {"announcements","financials"}


def test_historical_universe_and_theme_membership_are_as_of_safe():
    store = PointInTimeStore(
        securities=[SecurityHistory("OLD.SH","退市样本",date(2010,1,1),date(2024,6,1)),
                    SecurityHistory("NEW.SH","新股",date(2025,1,1))],
        memberships=[ThemeMembership("OLD.SH","旧能源",date(2020,1,1),None,
                                     datetime(2020,1,1,tzinfo=SH),datetime(2020,1,1,tzinfo=SH))])
    assert [x.symbol for x in store.universe_as_of(date(2024,5,1))] == ["OLD.SH"]
    assert store.universe_as_of(date(2024,7,1)) == ()
    assert store.securities[0].symbol == "OLD.SH"  # delisted history is retained, not deleted
    assert store.theme_as_of("OLD.SH",date(2024,5,1)) == "旧能源"


def write_csv(path, fieldnames, rows):
    with path.open("w",encoding="utf-8",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=fieldnames);writer.writeheader();writer.writerows(rows)


def make_bundle(root, *, authorized=True, pit_verified=True, stale_financial=False):
    root.mkdir()
    write_csv(root/"securities.csv",["symbol","name","listed_at","delisted_at","board"],[
        {"symbol":"LIVE.SH","name":"存续公司","listed_at":"2020-01-01","delisted_at":"","board":"主板"},
        {"symbol":"OLD.SH","name":"退市样本","listed_at":"2010-01-01","delisted_at":"2025-06-01","board":"主板"}])
    write_csv(root/"theme_memberships.csv",["symbol","theme","effective_from","effective_to","published_at","available_at"],[
        {"symbol":"LIVE.SH","theme":"设备","effective_from":"2020-01-01","effective_to":"","published_at":"2020-01-01T09:00:00+08:00","available_at":"2020-01-01T09:01:00+08:00"},
        {"symbol":"OLD.SH","theme":"旧能源","effective_from":"2020-01-01","effective_to":"","published_at":"2020-01-01T09:00:00+08:00","available_at":"2020-01-01T09:01:00+08:00"}])
    fields=["symbol","date","open","high","low","close","volume","amount","industry","published_at","effective_at","available_at"]
    write_csv(root/"bars.csv",fields,[
        {"symbol":"LIVE.SH","date":"2025-01-03","open":10,"high":11,"low":9,"close":10.5,"volume":100000,"amount":1050000,"industry":"机械","published_at":"2025-01-03T15:01:00+08:00","effective_at":"2025-01-03T15:00:00+08:00","available_at":"2025-01-03T15:05:00+08:00"},
        {"symbol":"OLD.SH","date":"2025-01-03","open":5,"high":5.5,"low":4.8,"close":5.2,"volume":80000,"amount":416000,"industry":"煤炭","published_at":"2025-01-03T15:01:00+08:00","effective_at":"2025-01-03T15:00:00+08:00","available_at":"2025-01-03T15:05:00+08:00"},
        {"symbol":"LIVE.SH","date":"2025-01-06","open":11,"high":12,"low":10,"close":11.5,"volume":100000,"amount":1150000,"industry":"机械","published_at":"2025-01-06T15:01:00+08:00","effective_at":"2025-01-06T15:00:00+08:00","available_at":"2025-01-06T15:05:00+08:00"},
        {"symbol":"OLD.SH","date":"2025-01-06","open":5.2,"high":5.4,"low":5.0,"close":5.3,"volume":75000,"amount":397500,"industry":"煤炭","published_at":"2025-01-06T15:01:00+08:00","effective_at":"2025-01-06T15:00:00+08:00","available_at":"2025-01-06T15:05:00+08:00"}])
    signed={"batch_id":"batch-001","authorization":{"authorized":authorized,"scope":"internal-research" if authorized else ""},
            "pit":{"verified":pit_verified,"method":"vendor-publication-timestamp-audit" if pit_verified else ""},
            "datasets":{"bars":{"as_of":"2025-01-06T18:00:00+08:00","max_age_hours":72,"required":True},
                        "financials":{"as_of":"2024-01-01T18:00:00+08:00" if stale_financial else "2025-01-06T18:00:00+08:00","max_age_hours":240,"required":True}}}
    manifest=RawBatchManifest.create(root,batch_id="batch-001",provider="licensed-vendor",created_at=datetime(2025,1,6,19,tzinfo=SH),
                                     files=("bars.csv","securities.csv","theme_memberships.csv"),metadata=signed)
    (root/"metadata.json").write_text(json.dumps({**signed,"manifest":asdict(manifest)},ensure_ascii=False),"utf-8")
    return root


def test_licensed_bundle_requires_hash_authorization_and_pit(tmp_path):
    provider=LicensedCsvBundleProvider(make_bundle(tmp_path/"good"))
    assert provider.status()["production_ready"] is True
    snapshot=provider.load(date(2025,7,1))
    assert snapshot.metadata["production_ready"] is True
    assert {x.symbol for x in snapshot.bars} == {"LIVE.SH","OLD.SH"}  # delisted history retained in research bars
    assert snapshot.metadata["active_security_count"] == 1

    no_auth=LicensedCsvBundleProvider(make_bundle(tmp_path/"no-auth",authorized=False))
    assert no_auth.status()["production_ready"] is False
    blocked=check_quality(no_auth.load(date(2025,1,6)),datetime(2025,1,6,20,tzinfo=SH))
    assert blocked.status=="blocked" and any(x.code=="NOT_PRODUCTION_READY" for x in blocked.issues)


def test_as_of_rebuild_excludes_future_available_bar(tmp_path):
    provider=LicensedCsvBundleProvider(make_bundle(tmp_path/"bundle"))
    early=provider.load(date(2025,1,3)); late=provider.load(date(2025,1,6))
    assert max(x.day for x in early.bars)==date(2025,1,3)
    assert max(x.day for x in late.bars)==date(2025,1,6)


def test_raw_batch_tampering_is_detected(tmp_path):
    root=make_bundle(tmp_path/"tamper");provider=LicensedCsvBundleProvider(root)
    assert provider.status()["manifest_valid"] is True
    with (root/"bars.csv").open("a",encoding="utf-8") as handle: handle.write("tampered\n")
    status=provider.status()
    assert status["manifest_valid"] is False and "tampered:bars.csv" in status["manifest_errors"]
    assert status["production_ready"] is False


def test_independent_dataset_freshness_blocks_required_but_warns_optional(tmp_path):
    snapshot=LicensedCsvBundleProvider(make_bundle(tmp_path/"stale",stale_financial=True)).load(date(2025,1,6))
    report=check_quality(snapshot,datetime(2025,1,7,18,tzinfo=SH),max_age_hours=72)
    assert report.status=="blocked"
    assert any(x.code=="DATASET_STALE:financials" for x in report.issues)
    snapshot.metadata["datasets"]["financials"]["required"]=False
    report=check_quality(snapshot,datetime(2025,1,7,18,tzinfo=SH),max_age_hours=72)
    assert report.status=="warning"
    assert any(x.code=="DATASET_DEGRADED:financials" for x in report.issues)
