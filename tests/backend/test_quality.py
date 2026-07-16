from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from dataclasses import replace

from quant_system.models import DataSnapshot
from quant_system.providers import DeterministicDemoProvider
from quant_system.quality import check_quality
from quant_system.service import QuantService


def test_stale_data_blocks_publication_quality_gate():
    snap=DeterministicDemoProvider().load(date(2026,1,5));now=datetime(2026,1,10,tzinfo=ZoneInfo("Asia/Shanghai"))
    q=check_quality(snap,now)
    assert q.freshness=="stale" and q.status=="blocked"
    assert any(i.code=="STALE" for i in q.issues)


def test_persisted_decision_ages_to_stale_at_read_time_without_provider_reload():
    latest={"as_of":"2026-07-01T18:00:00+08:00",
            "quality":{"freshness":"fresh","status":"healthy","issues":[]}}
    now=datetime(2026,7,5,18,tzinfo=ZoneInfo("Asia/Shanghai"))

    view=QuantService._runtime_freshness_view(latest,now)

    assert latest["quality"]["freshness"]=="fresh"
    assert view["quality"]["freshness"]=="stale"
    assert view["quality"]["status"]=="blocked"
    assert view["quality"]["age_hours"]==96.0
    assert any(issue["code"]=="STALE" for issue in view["quality"]["issues"])


def test_missing_data_is_blocked():
    now=datetime.now(ZoneInfo("Asia/Shanghai"));q=check_quality(DataSnapshot(now,[],"x",10),now)
    assert q.status=="blocked"


def test_duplicate_future_and_trade_state_inconsistency_fail_closed():
    now=datetime(2026,7,3,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    last=snapshot.bars[-1]
    snapshot.bars.extend([
        last,
        replace(last,day=date(2026,7,4)),
        replace(last,symbol="HALT",suspended=True,volume=100,amount=1000),
    ])
    codes={issue.code for issue in check_quality(snapshot,now).issues}
    assert {"DUPLICATE_BAR","FUTURE_BAR","TRADE_STATE_INCONSISTENT"}<=codes


def test_declared_session_gap_and_extreme_adjustment_jump_fail_closed():
    now=datetime(2026,7,3,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    first=snapshot.bars[0]
    snapshot.bars[0]=replace(first,adj_factor=100)
    snapshot.metadata["expected_trading_days"]=["2026-06-28","2026-07-01","2026-07-02","2026-07-03"]
    codes={issue.code for issue in check_quality(snapshot,now).issues}
    assert "ADJ_FACTOR_JUMP" in codes
    assert "MISSING_TRADING_SESSION" in codes


def test_latest_session_coverage_uses_expected_universe_denominator():
    now=datetime(2026,7,3,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    latest=max(bar.day for bar in snapshot.bars)
    latest_symbols=sorted({bar.symbol for bar in snapshot.bars if bar.day==latest})
    # Simulate two members of a ten-name target universe missing on the latest
    # session. Exactly 80% is accepted; dropping one more must fail closed.
    base=[bar for bar in snapshot.bars if not (bar.day==latest and bar.symbol in latest_symbols[8:])]
    at_boundary=replace(snapshot,bars=base,expected_symbols=10)
    assert not any(issue.code=="LOW_COVERAGE" for issue in check_quality(at_boundary,now).issues)

    below=replace(at_boundary,bars=[bar for bar in base if not (bar.day==latest and bar.symbol==latest_symbols[7])])
    report=check_quality(below,now)
    assert report.status=="blocked"
    assert any(issue.code=="LOW_COVERAGE" and "7/10" in issue.message for issue in report.issues)


def test_missing_optional_dataset_is_explicitly_degraded_not_silently_healthy():
    now=datetime(2026,7,3,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    snapshot.metadata["datasets"]={
        "bars":{"required":True,"as_of":now.isoformat()},
        "theme_memberships":{"required":False,"available":False},
        "financials":{"required":False,"as_of":None},
    }

    report=check_quality(snapshot,now)
    assert report.status=="warning"
    assert {issue.code for issue in report.issues} >= {
        "DATASET_DEGRADED:theme_memberships",
        "DATASET_DEGRADED:financials",
    }
    assert not any(issue.severity=="error" for issue in report.issues)


def test_missing_required_dataset_asof_remains_blocking():
    now=datetime(2026,7,3,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    snapshot.metadata["datasets"]={"bars":{"required":True,"available":False}}

    report=check_quality(snapshot,now)
    assert report.status=="blocked"
    assert any(issue.code=="DATASET_ASOF_MISSING:bars" and issue.severity=="error" for issue in report.issues)


def test_public_adjustment_and_grouping_are_required_but_optional_enrichments_degrade():
    now=datetime(2026,7,3,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    snapshot.provider="tushare"
    snapshot.metadata.update({
        "production_ready":False,
        "enrichments":{
            "adj_factor":{"status":"unavailable","missing_bar_rows":len(snapshot.bars)},
            "daily_basic":{"status":"unavailable"},
            "index_daily":{"status":"unavailable"},
        },
    })
    snapshot.bars=[replace(bar,theme="行业未分类",industry="行业未分类") for bar in snapshot.bars]

    report=check_quality(snapshot,now)
    by_code={issue.code:issue for issue in report.issues}
    assert by_code["ENRICHMENT_REQUIRED:adj_factor"].severity=="error"
    assert by_code["GROUPING_UNAVAILABLE"].severity=="error"
    assert by_code["ENRICHMENT_DEGRADED:daily_basic"].severity=="warning"
    assert by_code["ENRICHMENT_DEGRADED:index_daily"].severity=="warning"
    assert report.status=="blocked"


def test_complete_public_adjustment_and_groups_leave_only_nonproduction_publication_block():
    now=datetime(2026,7,3,18,tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    snapshot.provider="tushare"
    snapshot.metadata.update({
        "production_ready":False,
        "enrichments":{
            "adj_factor":{"status":"available"},
            "daily_basic":{"status":"available"},
            "index_daily":{"status":"available"},
        },
    })

    report=check_quality(snapshot,now)
    errors={issue.code for issue in report.issues if issue.severity=="error"}
    assert errors=={"NOT_PRODUCTION_READY"}
