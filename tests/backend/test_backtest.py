from quant_system.backtest import run_backtest
from quant_system.providers import DeterministicDemoProvider


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

