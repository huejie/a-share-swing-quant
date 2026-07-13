from datetime import date
from dataclasses import replace
from quant_system.engine import _history,_return,assess_market,assess_stocks,assess_themes,build_portfolio,entry_signal,evaluate_exit,trailing_stop,update_trailing_stop
from quant_system.providers import DeterministicDemoProvider


def test_demo_provider_is_deterministic():
    a=DeterministicDemoProvider().load(date(2026,7,3));b=DeterministicDemoProvider().load(date(2026,7,3))
    assert a.bars==b.bars and len(a.bars)>1000


def test_scoring_and_small_portfolio_constraints():
    snap=DeterministicDemoProvider().load(); market=assess_market(snap); themes=assess_themes(snap); stocks=assess_stocks(snap,themes)
    portfolio=build_portfolio(snap,market,stocks,target_count=4)
    assert 3<=len(portfolio)<=5
    assert len({x.theme for x in portfolio})>=2
    assert all(x.target_weight<=.25 for x in portfolio)
    assert all(sum(1 for y in portfolio if y.theme==x.theme)<=2 for x in portfolio)
    assert sum(x.target_weight for x in portfolio)<=market.exposure_cap+.001
    industries={x.symbol:x.industry for x in stocks}
    assert all(sum(x.target_weight for x in portfolio if industries[x.symbol]==industry)<=.4501 for industry in set(industries.values()))
    assert themes==sorted(themes,key=lambda x:x.score,reverse=True)


def test_all_main_board_public_watchlist_degrades_to_balanced_style_without_crashing():
    snap=DeterministicDemoProvider().load(date(2026,7,3))
    main_only=replace(snap,bars=[replace(bar,board="主板") for bar in snap.bars])

    market=assess_market(main_only)

    assert market.style=="均衡"
    assert 0<=market.score<=100


def test_explicit_public_grouping_supports_diversified_three_to_five_name_portfolio():
    snap=DeterministicDemoProvider().load(date(2026,7,3))
    # The strategy must consume provider-supplied industry/theme labels rather
    # than collapsing every public-source security into one placeholder group.
    symbols=sorted({bar.symbol for bar in snap.bars})
    mapping={symbol:(f"题材-{index % 4}",f"行业-{index % 6}") for index,symbol in enumerate(symbols)}
    grouped=replace(snap,bars=[replace(bar,theme=mapping[bar.symbol][0],industry=mapping[bar.symbol][1]) for bar in snap.bars])
    market=assess_market(grouped)
    themes=assess_themes(grouped)
    stocks=assess_stocks(grouped,themes)
    portfolio=build_portfolio(grouped,market,stocks,target_count=4)

    assert 3<=len(portfolio)<=5
    assert len({position.theme for position in portfolio})>=2
    assert all(position.theme!="未配置" for position in portfolio)
    by_symbol={stock.symbol:stock for stock in stocks}
    assert all(by_symbol[position.symbol].industry!="未配置" for position in portfolio)


def test_adjustment_factor_removes_false_corporate_action_return_and_keeps_latest_price():
    snap=DeterministicDemoProvider().load(date(2026,7,3))
    symbol=snap.bars[0].symbol
    symbol_bars=[bar for bar in snap.bars if bar.symbol==symbol]
    split_day=symbol_bars[-30].day
    transformed=[]
    for bar in snap.bars:
        if bar.symbol!=symbol:
            transformed.append(bar)
        elif bar.day<split_day:
            transformed.append(replace(bar,open=100,high=101,low=99,close=100,adj_factor=1))
        else:
            transformed.append(replace(bar,open=50,high=50.5,low=49.5,close=50,adj_factor=2))
    history=_history(replace(snap,bars=transformed))[symbol]

    assert history[-1].close==50
    assert history[0].close==50
    assert _return(history,60)==0
    signal,zone=entry_signal(history)
    assert signal in {"平台突破","趋势回踩"}
    assert 48<=zone[0]<=51
    assert 49<=zone[1]<=52


def test_profit_giveback_stop_activates_only_after_15_percent():
    assert trailing_stop(100,114.99) is None
    assert trailing_stop(100,130)==121
    decision=evaluate_exit(entry=100,peak=130,close=120,initial_stop=90,holding_days=30)
    assert decision.should_exit and decision.priority==6 and "回吐30%" in decision.reason


def test_hard_risk_has_exit_priority_over_price_stop():
    decision=evaluate_exit(entry=100,peak=110,close=80,initial_stop=90,holding_days=20,hard_risk=True)
    assert decision.priority==1


def test_capacity_can_exclude_stock_for_large_account():
    snap=DeterministicDemoProvider().load(); stocks=assess_stocks(snap,capital=10_000_000)
    assert all(x.excluded_reason is None or "容量" in x.excluded_reason for x in stocks)


def test_trailing_stop_is_monotonic_and_corporate_action_adjusted():
    assert update_trailing_stop(121,100,128)==121
    assert update_trailing_stop(121,100,140)==128
    assert update_trailing_stop(121,50,65,.5)==60.5
    assert evaluate_exit(entry=50,peak=65,close=60,initial_stop=45,holding_days=20,
                         previous_protective=121,corporate_action_price_ratio=.5).protective_price==60.5


def test_all_documented_hard_risk_filters_are_explicit():
    snap=DeterministicDemoProvider().load(); last_day=max(x.day for x in snap.bars); symbol=snap.bars[0].symbol
    for field,reason in [("is_delisting","退市"),("regulatory_risk","监管"),("audit_abnormal","审计"),("event_risk","事件")]:
        bars=[replace(x,**{field:True}) if x.symbol==symbol and x.day==last_day else x for x in snap.bars]
        changed=replace(snap,bars=bars)
        item=next(x for x in assess_stocks(changed) if x.symbol==symbol)
        assert not item.eligible and reason in item.excluded_reason
