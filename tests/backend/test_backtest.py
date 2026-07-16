from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import quant_system.backtest as backtest_module
import pytest
from quant_system.backtest import apply_rebalance_policy, run_backtest
from quant_system.engine import MODEL_VERSION, assess_market, assess_stocks, assess_themes
from quant_system.models import Bar, DataSnapshot, jsonable
from quant_system.portfolio_policy import weekly_theme_names
from quant_system.providers import DeterministicDemoProvider
from quant_system.repository import SQLiteRepository
from quant_system.service import QuantService


def test_backtest_is_repeatable_and_next_day_only():
    snap=DeterministicDemoProvider().load();a=run_backtest(snap);b=run_backtest(snap)
    assert a.final_equity==b.final_equity and a.total_return==b.total_return
    assert all(x.fill_day>x.signal_day for x in a.fills)
    assert all(x.shares%100==0 for x in a.fills)
    assert a.assumptions["automatic_trading"] is False


def test_backtest_has_costed_equity_curve():
    result=run_backtest(DeterministicDemoProvider().load(),capital=100_000)
    assert result.equity_curve and result.final_equity>0
    assert all(x.fee>=5 for x in result.fills)
    assert -1<result.max_drawdown<=0
    assert result.assumptions["market_inputs_degraded"] is True
    assert result.assumptions["production_holding_buffer_shared"] is True
    assert "BT-001-PRODUCTION-HOLDING-BUFFER-NOT-SHARED" not in result.assumptions.get("research_gate_failures",[])


def test_transaction_cost_multiplier_is_applied_by_shared_engine(monkeypatch):
    snapshot=_flat_snapshot()
    advice=SimpleNamespace(symbol="TEST.SH",target_weight=.20,initial_weight=.12,initial_stop=9.0)
    _patch_flat_strategy(monkeypatch,lambda *_args,**_kwargs:[advice])

    base=run_backtest(snapshot,capital=1_000_000,slippage_bps=0,transaction_cost_multiplier=1)
    stressed=run_backtest(snapshot,capital=1_000_000,slippage_bps=0,transaction_cost_multiplier=2)

    assert base.fills and stressed.fills
    assert stressed.fills[0].shares==base.fills[0].shares
    assert stressed.fills[0].fee==pytest.approx(base.fills[0].fee*2)
    assert stressed.assumptions["transaction_cost_multiplier"]==2


def _flat_snapshot(days: int=85, *, volume: int=100_000_000, metadata=None) -> DataSnapshot:
    start=date(2026,1,1)
    bars=[Bar("TEST.SH","测试",start+timedelta(days=index),10,10.2,9.8,10,volume,
              10*volume,"测试题材","测试行业") for index in range(days)]
    return DataSnapshot(datetime.combine(bars[-1].day,datetime.min.time(),timezone.utc),
                        bars,"backtest-fixture",1,metadata or {})


def _patch_flat_strategy(monkeypatch, portfolio_factory):
    monkeypatch.setattr(backtest_module,"assess_market",lambda *_args,**_kwargs:SimpleNamespace(exposure_cap=.90))
    monkeypatch.setattr(backtest_module,"assess_themes",lambda *_args,**_kwargs:[])
    monkeypatch.setattr(backtest_module,"assess_stocks",lambda *_args,**_kwargs:[])
    monkeypatch.setattr(backtest_module,"build_portfolio",portfolio_factory)


def test_historical_subsnapshot_uses_same_day_close_and_never_final_market_inputs(monkeypatch):
    snapshot=_flat_snapshot(metadata={
        "market_inputs":{"global_risk_score":99.0,"source":"final future value"},
        "market_inputs_history":{
            "2026-01-01":{"global_risk_score":21.0,"source":"visible history"},
        },
    })
    captured=[]

    def market(subsnapshot,*_args,**_kwargs):
        captured.append(subsnapshot)
        return SimpleNamespace(exposure_cap=.90)

    monkeypatch.setattr(backtest_module,"assess_market",market)
    monkeypatch.setattr(backtest_module,"assess_themes",lambda *_args,**_kwargs:[])
    monkeypatch.setattr(backtest_module,"assess_stocks",lambda *_args,**_kwargs:[])
    monkeypatch.setattr(backtest_module,"build_portfolio",lambda *_args,**_kwargs:[])

    result=run_backtest(snapshot)

    assert captured
    assert all(item.as_of.date()==max(bar.day for bar in item.bars) for item in captured)
    assert all(item.as_of.hour==15 and item.as_of.utcoffset() is not None for item in captured)
    assert all(item.metadata["market_inputs"]["global_risk_score"]==21.0 for item in captured)
    assert all(item.metadata["market_inputs"]["global_risk_score"]!=99.0 for item in captured)
    assert result.assumptions["market_inputs_degraded"] is False


def test_new_position_uses_initial_weight_then_later_confirmation_adds_to_target(monkeypatch):
    snapshot=_flat_snapshot()
    advice=SimpleNamespace(symbol="TEST.SH",target_weight=.20,initial_weight=.12,initial_stop=9.0)
    _patch_flat_strategy(monkeypatch,lambda *_args,**_kwargs:[advice])

    result=run_backtest(snapshot,capital=1_000_000,slippage_bps=0)

    buys=[fill for fill in result.fills if fill.side=="buy"]
    assert len(buys)>=2
    assert buys[0].shares==12_000
    assert buys[1].signal_day>buys[0].signal_day
    stages=[event["stage"] for event in result.order_ledger if event["status"]=="created" and event["side"]=="buy"]
    assert stages[:2]==["initial","add_confirmation"]
    assert result.assumptions["two_stage_entry"]=="initial_weight_then_later_target_confirmation"


def test_buy_and_sell_capacity_remainders_persist_across_sessions(monkeypatch):
    snapshot=_flat_snapshot(days=100,volume=10_000)
    first_signal_day=sorted({bar.day for bar in snapshot.bars})[60]
    advice=SimpleNamespace(symbol="TEST.SH",target_weight=.20,initial_weight=.12,initial_stop=9.0)

    def portfolio(subsnapshot,*_args,**_kwargs):
        return [advice] if subsnapshot.as_of.date()==first_signal_day else []

    _patch_flat_strategy(monkeypatch,portfolio)
    result=run_backtest(snapshot,capital=100_000,slippage_bps=0)

    buys=[fill for fill in result.fills if fill.side=="buy"]
    sells=[fill for fill in result.fills if fill.side=="sell"]
    assert len(buys)>1 and len({fill.fill_day for fill in buys})>1
    assert len(sells)>1 and len({fill.fill_day for fill in sells})>1
    assert all(fill.shares<=100 for fill in buys+sells)
    assert any(fill.status=="partial" for fill in buys)
    assert any(fill.status=="partial" for fill in sells)
    assert any(event["reason"]=="signal_no_longer_confirmed" and event["remaining_shares"]>0
               for event in result.order_ledger)
    assert result.assumptions["persistent_partial_orders"] is True


def test_delisted_or_unavailable_position_is_written_down_without_false_sell(monkeypatch):
    start=date(2026,1,1)
    bars=[]
    for index in range(75):
        day=start+timedelta(days=index)
        bars.append(Bar("OTHER.SH","市场日历",day,10,10.2,9.8,10,100_000_000,1_000_000_000,
                        "其他题材","其他行业"))
        if index<=62:
            bars.append(Bar("TEST.SH","退市样本",day,10,10.2,9.8,10,100_000_000,1_000_000_000,
                            "测试题材","测试行业",is_delisting=index==62))
    snapshot=DataSnapshot(datetime.combine(bars[-1].day,datetime.min.time(),timezone.utc),
                          bars,"delisting-fixture",2,{})
    advice=SimpleNamespace(symbol="TEST.SH",target_weight=.20,initial_weight=.12,initial_stop=9.0)
    _patch_flat_strategy(monkeypatch,lambda subsnapshot,*_args,**_kwargs:
                         [advice] if subsnapshot.as_of.date()<=start+timedelta(days=62) else [])

    result=run_backtest(snapshot,capital=100_000,slippage_bps=0)

    assert any(fill.side=="buy" for fill in result.fills)
    assert not any(fill.side=="sell" for fill in result.fills)
    write_down=next(event for event in result.order_ledger if event["status"]=="written_down")
    assert write_down["reason"]=="delisting_flag_then_permanent_quote_absence"
    assert result.assumptions["forced_risk_write_downs"]==1
    assert all(abs(point["cash"]+point["market_value"]-point["equity"])<.01
               for point in result.equity_curve)


def test_corporate_action_adjusts_shares_and_cost_basis_without_fake_split_return(monkeypatch):
    start=date(2026,1,1);bars=[]
    for index in range(85):
        price=5 if index>=70 else 10
        bars.append(Bar("TEST.SH","测试",start+timedelta(days=index),price,price,price,price,
                        100_000_000,price*100_000_000,"测试题材","测试行业",
                        share_multiplier=2.0 if index==70 else 1.0))
    snapshot=DataSnapshot(datetime.combine(bars[-1].day,datetime.min.time(),timezone.utc),bars,"fixture",1,{})
    advice=SimpleNamespace(symbol="TEST.SH",target_weight=.20,initial_weight=.12,initial_stop=9.0)
    _patch_flat_strategy(monkeypatch,lambda *_args,**_kwargs:[advice])

    result=run_backtest(snapshot,capital=100_000,slippage_bps=0)

    before=next(point for point in result.equity_curve if point["date"]==(start+timedelta(days=69)).isoformat())
    event_day=next(point for point in result.equity_curve if point["date"]==(start+timedelta(days=70)).isoformat())
    assert event_day["equity"]==pytest.approx(before["equity"],abs=.01)
    event=next(item for item in result.order_ledger if item.get("event")=="corporate_action")
    assert event["new_shares"]==event["old_shares"]*2 and event["adj_factor_not_used_for_accounting"] is True


def test_rebalance_target_decrease_creates_real_sell_delta(monkeypatch):
    snapshot=_flat_snapshot(days=90)
    first=sorted({bar.day for bar in snapshot.bars})[60]
    def portfolio(subsnapshot,*_args,**_kwargs):
        target=.20 if subsnapshot.as_of.date()==first else .10
        return [SimpleNamespace(symbol="TEST.SH",target_weight=target,initial_weight=.12,initial_stop=9.0)]
    _patch_flat_strategy(monkeypatch,portfolio)

    result=run_backtest(snapshot,capital=100_000,slippage_bps=0)

    assert any(fill.side=="sell" and fill.reason=="model_target_weight_decreased" for fill in result.fills)
    assert any(event.get("stage")=="target_reduction" and event["status"]=="created"
               for event in result.order_ledger)


def test_production_and_backtest_use_identical_buffered_policy_golden(tmp_path):
    snapshot=DeterministicDemoProvider().load(date(2026,7,3))
    market=assess_market(snapshot)
    themes=assess_themes(snapshot)
    selected=weekly_theme_names(snapshot,themes,None)
    stocks=assess_stocks(snapshot,themes,1_000_000,selected_themes=set(selected))
    initial,initial_turnover=apply_rebalance_policy(
        snapshot,market,stocks,previous_decision=None,capital=1_000_000,
        target_count=4,risk_per_trade=.0125,max_drawdown=.18,
    )
    assert initial
    previous={
        "id":"golden-prior","model_version":MODEL_VERSION,
        "data_timestamp":snapshot.as_of.isoformat(),
        "snapshot":{"portfolio":jsonable(initial),"selected_theme_names":selected},
        "turnover":initial_turnover,
    }
    service=QuantService(repository=SQLiteRepository(tmp_path/"shared-policy.db"))
    service.decisions=[previous]

    production_portfolio,production_turnover=service._buffered_portfolio(snapshot,market,stocks)
    backtest_portfolio,backtest_turnover=apply_rebalance_policy(
        snapshot,market,stocks,previous_decision=previous,capital=service.settings.capital,
        target_count=service.settings.target_count,risk_per_trade=service.settings.risk_per_trade,
        max_drawdown=service.settings.max_portfolio_drawdown,
    )

    assert [item.symbol for item in backtest_portfolio]==[item.symbol for item in production_portfolio]
    assert backtest_turnover==production_turnover
    assert [(item.symbol,item.action,item.target_weight) for item in backtest_portfolio]==[
        (item.symbol,item.action,item.target_weight) for item in production_portfolio
    ]
