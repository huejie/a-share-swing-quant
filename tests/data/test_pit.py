from dataclasses import asdict
from datetime import date, datetime
import csv
import json
from zoneinfo import ZoneInfo

import pytest

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
    fields=["symbol","date","open","high","low","close","volume","amount","industry","published_at","effective_at","collected_at","available_at",
            "is_st","is_delisting","regulatory_risk","audit_abnormal","event_risk","adj_factor","limit_up","limit_down","suspended",
            "listed_trading_days","free_float_market_cap","schema_version","source_ref"]
    bar_rows=[
        {"symbol":"LIVE.SH","date":"2025-01-03","open":10,"high":11,"low":9,"close":10.5,"volume":100000,"amount":1050000,"industry":"机械","published_at":"2025-01-03T15:01:00+08:00","effective_at":"2025-01-03T15:00:00+08:00","available_at":"2025-01-03T15:05:00+08:00","is_st":False,"is_delisting":False,"regulatory_risk":False,"audit_abnormal":False,"event_risk":False,"adj_factor":1.25,"limit_up":False,"limit_down":False,"suspended":False},
        {"symbol":"OLD.SH","date":"2025-01-03","open":5,"high":5.5,"low":4.8,"close":5.2,"volume":80000,"amount":416000,"industry":"煤炭","published_at":"2025-01-03T15:01:00+08:00","effective_at":"2025-01-03T15:00:00+08:00","available_at":"2025-01-03T15:05:00+08:00","is_st":False,"is_delisting":False,"regulatory_risk":False,"audit_abnormal":False,"event_risk":False,"adj_factor":.8,"limit_up":False,"limit_down":False,"suspended":False},
        {"symbol":"LIVE.SH","date":"2025-01-06","open":11,"high":12,"low":10,"close":11.5,"volume":100000,"amount":1150000,"industry":"机械","published_at":"2025-01-06T15:01:00+08:00","effective_at":"2025-01-06T15:00:00+08:00","available_at":"2025-01-06T15:05:00+08:00","is_st":False,"is_delisting":False,"regulatory_risk":False,"audit_abnormal":False,"event_risk":False,"adj_factor":1.25,"limit_up":False,"limit_down":False,"suspended":False},
        {"symbol":"OLD.SH","date":"2025-01-06","open":5.2,"high":5.4,"low":5.0,"close":5.3,"volume":75000,"amount":397500,"industry":"煤炭","published_at":"2025-01-06T15:01:00+08:00","effective_at":"2025-01-06T15:00:00+08:00","available_at":"2025-01-06T15:05:00+08:00","is_st":False,"is_delisting":False,"regulatory_risk":False,"audit_abnormal":False,"event_risk":False,"adj_factor":.8,"limit_up":False,"limit_down":False,"suspended":False}]
    listed={"LIVE.SH":1300,"OLD.SH":3600}
    for row in bar_rows:
        row.update({"collected_at":row["available_at"],"listed_trading_days":listed[row["symbol"]],
                    "free_float_market_cap":8_000_000_000,"schema_version":"vendor-bars/v2",
                    "source_ref":f"vendor-bars:{row['symbol']}:{row['date']}"})
        listed[row["symbol"]]+=1
    write_csv(root/"bars.csv",fields,bar_rows)
    pit_fields=["dataset","entity_id","effective_at","published_at","collected_at","available_at","payload_json","revision","source_ref","parser_version"]
    pit_rows=[]
    for symbol,quality,catalyst,adj,risky in (("LIVE.SH",82,77,1.25,False),("OLD.SH",41,22,.8,True)):
        pit_rows.extend([
            {"dataset":"financials","entity_id":symbol,"effective_at":"2024-09-30T00:00:00+08:00","published_at":"2024-10-30T18:00:00+08:00","available_at":"2024-10-30T18:05:00+08:00","payload_json":json.dumps({"quality_score":quality,"audit_abnormal":risky}),"revision":1,"source_ref":f"vendor-financial:{symbol}:2024Q3"},
            {"dataset":"announcements","entity_id":symbol,"effective_at":"2024-12-20T00:00:00+08:00","published_at":"2024-12-20T18:00:00+08:00","available_at":"2024-12-20T18:05:00+08:00","payload_json":json.dumps({"catalyst_score":catalyst,"event_risk":risky,"regulatory_risk":risky,"is_delisting":False,"is_st":False}),"revision":1,"source_ref":f"vendor-announcement:{symbol}:baseline"},
            {"dataset":"corporate_actions","entity_id":symbol,"effective_at":"2024-01-01T00:00:00+08:00","published_at":"2024-01-01T08:00:00+08:00","available_at":"2024-01-01T08:05:00+08:00","payload_json":json.dumps({"adj_factor":adj}),"revision":1,"source_ref":f"vendor-action:{symbol}:base"},
        ])
    for effective,fund,valuation,risk in (("2025-01-01T00:00:00+08:00",61,57,35),("2025-01-04T00:00:00+08:00",48,54,72)):
        pit_rows.extend([
            {"dataset":"market_funding","entity_id":"A_SHARE_AND_MARGIN_ETF","effective_at":effective,"published_at":effective,"available_at":effective,"payload_json":json.dumps({"fund_flow_score":fund,"valuation_score":valuation}),"revision":1,"source_ref":f"vendor-market:{effective[:10]}"},
            {"dataset":"global_risk","entity_id":"GLOBAL_RISK","effective_at":effective,"published_at":effective,"available_at":effective,"payload_json":json.dumps({"global_risk_score":risk}),"revision":1,"source_ref":f"vendor-global:{effective[:10]}"},
        ])
    for row in pit_rows:
        row["collected_at"]=row["available_at"]
        row["parser_version"]="vendor-parser/2.1"
        payload=json.loads(row["payload_json"])
        if row["dataset"]=="corporate_actions":
            payload.update({"event_type":"cumulative_baseline","share_multiplier":1.0,
                            "cash_dividend_per_share":0.0})
        elif row["dataset"]=="announcements":
            payload.update({"event_type":"risk_and_catalyst_baseline","event_date":"2024-12-20",
                            "raw_text_ref":row["source_ref"],"parser_version":"announcement-parser/3.0"})
        elif row["dataset"]=="market_funding":
            payload.update({"margin_balance":1_500_000_000_000,"margin_balance_change":.012,
                            "etf_share_change":.008,"market_breadth":.61})
        elif row["dataset"]=="global_risk":
            payload.update({"global_equity":5200.0,"usd_cny":7.18,"interest_rate":4.25,
                            "volatility_index":18.5,"commodity_index":102.4})
        row["payload_json"]=json.dumps(payload)
    write_csv(root/"pit_records.csv",pit_fields,pit_rows)
    signed={"batch_id":"batch-001","authorization":{"authorized":authorized,
            "scope":"personal-research-and-derived-decision-display" if authorized else "",
            "reference":"contract-2025-001" if authorized else "","valid_until":"2030-12-31",
            "permitted_uses":["research","decision_support","derived_output_display"] if authorized else []},
            "pit":{"verified":pit_verified,"method":"vendor-publication-timestamp-audit" if pit_verified else ""},
            "datasets":{name:{"as_of":"2025-01-06T18:00:00+08:00","max_age_hours":240,"required":True,
                              "source_ref":f"contract-2025-001:{name}","schema_version":f"{name}/v2"}
                        for name in LicensedCsvBundleProvider.required_datasets}}
    if stale_financial: signed["datasets"]["financials"]["as_of"]="2024-01-01T18:00:00+08:00"
    manifest=RawBatchManifest.create(root,batch_id="batch-001",provider="licensed-vendor",created_at=datetime(2025,1,6,19,tzinfo=SH),
                                     files=("bars.csv","securities.csv","theme_memberships.csv","pit_records.csv"),metadata=signed)
    (root/"metadata.json").write_text(json.dumps({**signed,"manifest":asdict(manifest)},ensure_ascii=False),"utf-8")
    return root


def resign_bundle(root, *, files=("bars.csv","securities.csv","theme_memberships.csv","pit_records.csv")):
    signed={key:value for key,value in json.loads((root/"metadata.json").read_text("utf-8")).items() if key!="manifest"}
    manifest=RawBatchManifest.create(root,batch_id="batch-001",provider="licensed-vendor",
                                     created_at=datetime(2025,1,6,19,tzinfo=SH),files=files,metadata=signed)
    (root/"metadata.json").write_text(json.dumps({**signed,"manifest":asdict(manifest)}),"utf-8")


def test_licensed_bundle_requires_hash_authorization_and_pit(tmp_path):
    provider=LicensedCsvBundleProvider(make_bundle(tmp_path/"good"))
    status=provider.status()
    assert status["production_ready"] is True
    assert status["point_in_time_verified"] is True
    assert status["production_data_authorized"] is True
    assert status["research_eligible"] is True
    snapshot=provider.load(date(2025,7,1))
    assert snapshot.metadata["production_ready"] is True
    assert snapshot.metadata["point_in_time_verified"] is True
    assert snapshot.metadata["production_data_authorized"] is True
    assert {x.symbol for x in snapshot.bars} == {"LIVE.SH","OLD.SH"}  # delisted history retained in research bars
    assert snapshot.metadata["active_security_count"] == 1
    latest={bar.symbol:bar for bar in snapshot.bars if bar.day==date(2025,1,6)}
    assert latest["LIVE.SH"].quality == 82 and latest["LIVE.SH"].catalyst == 77
    assert latest["LIVE.SH"].event_date=="2024-12-20"
    assert latest["LIVE.SH"].event_source_ref.startswith("vendor-announcement:")
    assert latest["LIVE.SH"].event_parser_version=="announcement-parser/3.0"
    assert latest["OLD.SH"].audit_abnormal is True and latest["OLD.SH"].regulatory_risk is True
    assert latest["LIVE.SH"].adj_factor == 1.25 and latest["OLD.SH"].adj_factor == .8
    assert all(bar.listed_days>=120 for bar in snapshot.bars)
    assert all(bar.free_float_market_cap==8_000_000_000 for bar in snapshot.bars)
    assert all(bar.share_multiplier==1 and bar.cash_dividend_per_share==0 for bar in snapshot.bars)
    assert snapshot.metadata["market_inputs_history"]["2025-01-03"]["fund_flow_score"] == 61
    assert snapshot.metadata["market_inputs_history"]["2025-01-06"]["global_risk_score"] == 72
    assert snapshot.metadata["market_inputs_history"]["2025-01-06"]["market_funding_components"]["margin_balance"]>0
    assert snapshot.metadata["market_inputs_history"]["2025-01-06"]["global_risk_components"]["volatility_index"]==18.5
    assert snapshot.metadata["market_inputs"] == snapshot.metadata["market_inputs_history"]["2025-01-06"]
    assert snapshot.metadata["pit_materialization"]["status"] == "complete"
    assert snapshot.metadata["pit_records_visible"]
    assert check_quality(snapshot,datetime(2025,1,6,20,tzinfo=SH)).status == "healthy"

    no_auth=LicensedCsvBundleProvider(make_bundle(tmp_path/"no-auth",authorized=False))
    no_auth_status=no_auth.status()
    assert no_auth_status["production_ready"] is False
    assert no_auth_status["point_in_time_verified"] is True
    assert no_auth_status["production_data_authorized"] is False
    assert no_auth_status["research_eligible"] is False
    blocked=check_quality(no_auth.load(date(2025,1,6)),datetime(2025,1,6,20,tzinfo=SH))
    assert blocked.status=="blocked" and any(x.code=="NOT_PRODUCTION_READY" for x in blocked.issues)

    no_pit=LicensedCsvBundleProvider(make_bundle(tmp_path/"no-pit",pit_verified=False))
    no_pit_status=no_pit.status()
    assert no_pit_status["point_in_time_verified"] is False
    assert no_pit_status["production_data_authorized"] is True
    assert no_pit_status["research_eligible"] is False


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
    assert status["point_in_time_verified"] is False
    assert status["production_data_authorized"] is False
    assert status["research_eligible"] is False


def test_bundle_contract_rejects_missing_pit_dataset_and_incomplete_bar_fields(tmp_path):
    root=make_bundle(tmp_path/"missing-records")
    with (root/"pit_records.csv").open(encoding="utf-8") as handle: rows=list(csv.DictReader(handle))
    write_csv(root/"pit_records.csv",rows[0].keys(),[row for row in rows if row["dataset"]!="global_risk"])
    resign_bundle(root)
    status=LicensedCsvBundleProvider(root).status()
    assert status["production_ready"] is False
    assert "pit_dataset_missing:global_risk" in status["contract_errors"]

    incomplete=tmp_path/"incomplete-bars"
    make_bundle(incomplete)
    with (incomplete/"bars.csv").open(encoding="utf-8") as handle: rows=list(csv.DictReader(handle))
    fields=[name for name in rows[0] if name!="suspended"]
    write_csv(incomplete/"bars.csv",fields,[{key:value for key,value in row.items() if key in fields} for row in rows])
    resign_bundle(incomplete)
    status=LicensedCsvBundleProvider(incomplete).status()
    assert status["production_ready"] is False
    assert "bars_field_missing:suspended" in status["contract_errors"]


@pytest.mark.parametrize(("field","value","error_prefix"),[
    ("source_ref","","pit_source_ref_missing"),
    ("parser_version","","pit_parser_version_missing"),
    ("collected_at","not-a-time","pit_timestamp_invalid:collected_at"),
    ("available_at","not-a-time","pit_timestamp_invalid:available_at"),
    ("payload_json","{}","pit_payload_invalid"),
])
def test_pit_record_requires_traceable_source_timestamp_and_payload(tmp_path,field,value,error_prefix):
    root=make_bundle(tmp_path/field)
    with (root/"pit_records.csv").open(encoding="utf-8") as handle: rows=list(csv.DictReader(handle))
    rows[0][field]=value
    write_csv(root/"pit_records.csv",rows[0].keys(),rows)
    resign_bundle(root)
    status=LicensedCsvBundleProvider(root).status()
    assert status["production_ready"] is False
    assert any(error.startswith(error_prefix) for error in status["contract_errors"])


def test_manifest_must_cover_pit_records_even_when_file_exists(tmp_path):
    root=make_bundle(tmp_path/"manifest-gap")
    resign_bundle(root,files=("bars.csv","securities.csv","theme_memberships.csv"))
    status=LicensedCsvBundleProvider(root).status()
    assert status["manifest_valid"] is True
    assert status["production_ready"] is False
    assert "manifest_not_covering:pit_records.csv" in status["contract_errors"]


def test_bundle_requires_explicit_authorized_uses_and_raw_market_components(tmp_path):
    root=make_bundle(tmp_path/"uses")
    metadata=json.loads((root/"metadata.json").read_text("utf-8"))
    metadata["authorization"]["permitted_uses"]=["research"]
    (root/"metadata.json").write_text(json.dumps(metadata),"utf-8")
    resign_bundle(root)
    status=LicensedCsvBundleProvider(root).status()
    assert status["production_data_authorized"] is False
    assert "authorization_permitted_uses_incomplete" in status["authorization_errors"]

    raw=make_bundle(tmp_path/"raw-components")
    with (raw/"pit_records.csv").open(encoding="utf-8") as handle:rows=list(csv.DictReader(handle))
    global_row=next(row for row in rows if row["dataset"]=="global_risk")
    payload=json.loads(global_row["payload_json"]);payload.pop("volatility_index")
    global_row["payload_json"]=json.dumps(payload)
    write_csv(raw/"pit_records.csv",rows[0].keys(),rows);resign_bundle(raw)
    status=LicensedCsvBundleProvider(raw).status()
    assert status["production_ready"] is False
    assert any("pit_payload_field_missing:global_risk:volatility_index" in item
               for item in status["contract_errors"])


def test_bar_materialization_fails_when_record_was_not_visible_at_that_time(tmp_path):
    root=make_bundle(tmp_path/"late-record")
    with (root/"pit_records.csv").open(encoding="utf-8") as handle: rows=list(csv.DictReader(handle))
    late=next(row for row in rows if row["dataset"]=="announcements" and row["entity_id"]=="OLD.SH")
    late["published_at"]="2025-01-05T18:00:00+08:00"
    late["collected_at"]="2025-01-05T18:03:00+08:00"
    late["available_at"]="2025-01-05T18:05:00+08:00"
    write_csv(root/"pit_records.csv",rows[0].keys(),rows)
    resign_bundle(root)
    provider=LicensedCsvBundleProvider(root)
    status=provider.status()
    assert status["production_ready"] is False
    assert "bar_pit_not_visible:announcements:OLD.SH:2025-01-03" in status["contract_errors"]
    with pytest.raises(RuntimeError,match="production contract"):
        provider.load(date(2025,1,6))


def test_independent_dataset_freshness_blocks_required_but_warns_optional(tmp_path):
    snapshot=LicensedCsvBundleProvider(make_bundle(tmp_path/"stale",stale_financial=True)).load(date(2025,1,6))
    report=check_quality(snapshot,datetime(2025,1,7,18,tzinfo=SH),max_age_hours=72)
    assert report.status=="blocked"
    assert any(x.code=="DATASET_STALE:financials" for x in report.issues)
    snapshot.metadata["datasets"]["financials"]["required"]=False
    report=check_quality(snapshot,datetime(2025,1,7,18,tzinfo=SH),max_age_hours=72)
    assert report.status=="warning"
    assert any(x.code=="DATASET_DEGRADED:financials" for x in report.issues)
