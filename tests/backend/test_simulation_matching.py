from quant_system.models import Bar
from quant_system.repository import SQLiteRepository
from datetime import date
import pytest


def bar(symbol="TEST",open_=10,close=11,volume=1_000_000,**flags):
    return Bar(symbol,"测试",date(2026,7,7),open_,max(open_,close)*1.01,min(open_,close)*.99,close,volume,volume*close,"测试题材","测试行业",**flags)


def test_golden_buy_mark_sell_and_restart(tmp_path):
    path=tmp_path/"golden.db";repo=SQLiteRepository(path);repo.ensure_simulation_account(100_000)
    repo.append_simulation_intents("buy-run","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",
                                   [{"symbol":"TEST","side":"buy","amount":20_000,"target_weight":.2,"initial_weight":.2}])
    outcome=repo.match_pending("2026-07-07T18:00:00+08:00",{"TEST":bar()})
    assert outcome==[{"id":1,"symbol":"TEST","side":"buy","status":"filled","filled_quantity":1900,"reason":"ok"}]
    position=repo.simulation_positions()["TEST"]
    assert position["shares"]==1900
    valuation=repo.mark_to_market("2026-07-07",{"TEST":11},{"fixture":"golden"})
    assert valuation["market_value"]==20_900
    assert abs(valuation["cash"]+valuation["market_value"]-valuation["equity"])<.01
    # A new repository/service process sees the exact cash and position state.
    restarted=SQLiteRepository(path)
    assert restarted.simulation_positions()["TEST"]["shares"]==1900
    restarted.append_simulation_intents("sell-run","2026-07-07T18:00:00+08:00","2026-07-08T09:30:00+08:00",
                                        [{"symbol":"TEST","side":"sell","quantity":1900}])
    sold=restarted.match_pending("2026-07-08T18:00:00+08:00",{"TEST":bar(open_=12,close=12)})[0]
    assert sold["status"]=="filled" and sold["filled_quantity"]==1900
    assert restarted.simulation_positions()=={}
    sell_event=next(x for x in restarted.simulation()["ledger"] if x["run_key"]=="sell-run")
    assert sell_event["fee"]>5 and sell_event["payload"]["side"]=="sell"  # commission + sell stamp tax


def test_partial_and_rejected_constraints_are_persisted(tmp_path):
    repo=SQLiteRepository(tmp_path/"constraints.db");repo.ensure_simulation_account(100_000)
    repo.append_simulation_intents("partial","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",
                                   [{"symbol":"LOWVOL","side":"buy","amount":50_000}])
    partial=repo.match_pending("2026-07-07T18:00:00+08:00",{"LOWVOL":bar("LOWVOL",volume=10_000)})[0]
    assert partial["status"]=="partial" and partial["filled_quantity"]==100
    repo.append_simulation_intents("rejected","2026-07-07T18:00:00+08:00","2026-07-08T09:30:00+08:00",
                                   [{"symbol":"HALT","side":"buy","amount":20_000}])
    rejected=repo.match_pending("2026-07-08T18:00:00+08:00",{"HALT":bar("HALT",suspended=True)})[0]
    assert rejected["status"]=="rejected" and rejected["reason"]=="suspended_or_price_limit"
    records={x["run_key"]:x for x in repo.simulation()["ledger"]}
    assert records["partial"]["status"]=="partial" and records["rejected"]["status"]=="rejected"


def test_buy_fee_and_board_lot_are_exact(tmp_path):
    repo=SQLiteRepository(tmp_path/"fees.db");repo.ensure_simulation_account(100_000)
    repo.append_simulation_intents("fee","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",
                                   [{"symbol":"TEST","side":"buy","amount":20_000}])
    repo.match_pending("2026-07-07T18:00:00+08:00",{"TEST":bar()})
    event=repo.simulation()["ledger"][0]
    assert event["quantity"]%100==0 and event["price"]==10.008
    assert event["fee"]==round(max(5,event["amount"]*.0003),2)


@pytest.mark.parametrize(
    "side,flags,expected",
    [
        ("buy",{"limit_up":True},"rejected"),
        ("buy",{"limit_down":True},"filled"),
        ("sell",{"limit_down":True},"rejected"),
        ("sell",{"limit_up":True},"filled"),
        ("buy",{"suspended":True},"rejected"),
        ("sell",{"suspended":True},"rejected"),
    ],
)
def test_simulation_uses_directional_price_limit_and_suspension_rules(tmp_path,side,flags,expected):
    repo=SQLiteRepository(tmp_path / f"{side}-{next(iter(flags))}.db")
    repo.ensure_simulation_account(100_000)
    if side=="sell":
        repo.append_simulation_intents(
            "seed","2026-07-05T18:00:00+08:00","2026-07-06T09:30:00+08:00",
            [{"symbol":"TEST","side":"buy","amount":20_000}],
        )
        seeded=repo.match_pending("2026-07-06T18:00:00+08:00",{"TEST":bar()})[0]
        assert seeded["status"]=="filled"
        intent={"symbol":"TEST","side":"sell","quantity":seeded["filled_quantity"]}
    else:
        intent={"symbol":"TEST","side":"buy","amount":20_000}
    repo.append_simulation_intents(
        "directional","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",[intent],
    )

    outcome=repo.match_pending("2026-07-07T18:00:00+08:00",{"TEST":bar(**flags)})[0]

    assert outcome["status"]==expected
    if expected=="rejected":
        assert outcome["reason"]=="suspended_or_price_limit"
    else:
        assert outcome["reason"]=="ok"
