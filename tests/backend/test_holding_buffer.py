from dataclasses import replace
from datetime import date, datetime
from quant_system.engine import MODEL_VERSION, assess_market, assess_stocks, assess_themes
from quant_system.models import MarketRegime
from quant_system.providers import DeterministicDemoProvider
from quant_system.repository import SQLiteRepository
from quant_system.service import QuantService


OLD=["300760.SZ","688981.SH","300308.SZ","300750.SZ"]


def prior(day="2026-07-01T18:00:00+08:00",used=0,symbols=OLD):
    return {"id":"prior","data_timestamp":day,"model_version":MODEL_VERSION,
            "snapshot":{"portfolio":[{"symbol":x} for x in symbols]},
            "turnover":{"week_normal_replacements_used":used}}


def context(tmp_path,as_of=date(2026,7,3)):
    service=QuantService(repository=SQLiteRepository(tmp_path/"buffer.db"));service.decisions=[]
    snap=DeterministicDemoProvider().load(as_of);market=assess_market(snap);themes=assess_themes(snap);stocks=assess_stocks(snap,themes)
    return service,snap,market,stocks


def assert_constraints(portfolio,market,stocks=None,capital=1_000_000):
    assert 3<=len(portfolio)<=5
    assert sum(p.target_weight for p in portfolio)<=market.exposure_cap+.001
    assert all(p.target_weight<=.25 for p in portfolio)
    assert all(sum(1 for y in portfolio if y.theme==x.theme)<=2 for x in portfolio)
    assert len({x.theme for x in portfolio})>=2
    if stocks:
        by={x.symbol:x for x in stocks};industries={by[x.symbol].industry for x in portfolio}
        assert all(sum(x.target_weight for x in portfolio if by[x.symbol].industry==industry)<=.45+.001 for industry in industries)
        assert all(x.target_weight*capital<=by[x.symbol].avg_amount_20d*.02+.01 for x in portfolio)


def test_same_week_only_one_normal_replacement_and_current_prices(tmp_path):
    service,snap,market,stocks=context(tmp_path);service.decisions=[prior()]
    portfolio,turnover=service._buffered_portfolio(snap,market,stocks)
    normal=[x for x in turnover["replaced"] if x["kind"]=="normal"]
    assert len(normal)==1 and turnover["week_normal_replacements_used"]==1
    assert "300750.SZ" in {x["symbol"] for x in turnover["retained"]}  # second weak holding waits for a later week
    current={x.symbol:x.close for x in stocks}
    assert all(p.entry_price==current[p.symbol] for p in portfolio)
    assert_constraints(portfolio,market,stocks)


def test_budget_is_exhausted_for_later_day_in_same_iso_week(tmp_path):
    service,snap,market,stocks=context(tmp_path);service.decisions=[prior(used=1)]
    portfolio,turnover=service._buffered_portfolio(snap,market,stocks)
    assert turnover["replacement_budget"]==0
    assert not [x for x in turnover["replaced"] if x["kind"]=="normal"]
    assert_constraints(portfolio,market,stocks)


def test_cross_week_resets_to_exactly_one_and_recovery_can_fill_three(tmp_path):
    service,snap,market,stocks=context(tmp_path,date(2026,7,6));service.decisions=[prior(used=1)]
    portfolio,turnover=service._buffered_portfolio(snap,market,stocks)
    assert turnover["replacement_budget"]==1
    assert len([x for x in turnover["replaced"] if x["kind"]=="normal"])<=1
    assert_constraints(portfolio,market,stocks)
    service.decisions=[prior(symbols=OLD[:2])]
    recovered,recovery=service._buffered_portfolio(snap,market,stocks)
    assert recovery["exception"]=="recovery_to_three" and len(recovered)==3


def test_hard_risk_is_exempt_and_risk_off_can_exit_all(tmp_path):
    service,snap,market,stocks=context(tmp_path);service.decisions=[prior()]
    broken=[replace(x,eligible=False,excluded_reason="严重监管风险") if x.symbol==OLD[0] else x for x in stocks]
    portfolio,turnover=service._buffered_portfolio(snap,market,broken)
    hard=[x for x in turnover["replaced"] if x["kind"]=="hard_risk"]
    assert hard and hard[0]["reason"]=="严重监管风险"
    assert turnover["week_normal_replacements_used"]<=1
    riskoff=replace(market,regime=MarketRegime.EXTREME_RISK,exposure_cap=0)
    empty,audit=service._buffered_portfolio(snap,riskoff,stocks)
    assert empty==[] and audit["exception"]=="risk_off"
    assert len(audit["replaced"])==len(OLD)
