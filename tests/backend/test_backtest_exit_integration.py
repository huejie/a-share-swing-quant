"""Acceptance coverage proving that exit rules are wired into the event loop."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import quant_system.backtest as backtest_module
from quant_system.models import Bar, DataSnapshot


def test_profit_giveback_exit_is_executed_next_session(monkeypatch):
    """SIG-006/BT-001/BT-002: 100 -> 130 -> 120 must create a T+1 sell."""
    start = date(2026, 1, 1)
    bars = []
    for index in range(70):
        close = 100.0
        high = 101.0
        if index == 62:
            close, high = 130.0, 130.0
        elif index == 63:
            close, high = 120.0, 121.0
        bars.append(
            Bar(
                "TEST.SH", "测试", start + timedelta(days=index), close,
                high, min(close, 99.0), close, 10_000_000, close * 10_000_000,
                "测试题材", "测试行业",
            )
        )
    snapshot = DataSnapshot(
        datetime.combine(bars[-1].day, datetime.min.time(), timezone.utc),
        bars,
        "exit-integration-fixture",
        1,
    )

    monkeypatch.setattr(backtest_module, "assess_market", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(backtest_module, "assess_themes", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backtest_module, "assess_stocks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        backtest_module,
        "build_portfolio",
        lambda *_args, **_kwargs: [
            SimpleNamespace(symbol="TEST.SH", target_weight=0.20, initial_stop=90.0)
        ],
    )

    result = backtest_module.run_backtest(snapshot, rebalance_days=100, slippage_bps=0)

    sells = [fill for fill in result.fills if fill.side == "sell"]
    assert sells, "the daily backtest loop never executed the trailing-profit exit"
    assert sells[0].signal_day == start + timedelta(days=63)
    assert sells[0].fill_day == start + timedelta(days=64)
