from quant_system.models import Bar
from quant_system.engine import MODEL_VERSION
from quant_system.repository import SQLiteRepository
from quant_system.service import QuantService
from quant_system.providers import DeterministicDemoProvider
from datetime import date
import sqlite3
import pytest


def bar(symbol="TEST",open_=10,close=11,volume=1_000_000,**flags):
    return Bar(symbol,"测试",date(2026,7,7),open_,max(open_,close)*1.01,min(open_,close)*.99,close,volume,volume*close,"测试题材","测试行业",**flags)


def lineage(**extra):
    return {"model_version":MODEL_VERSION,"provider":"licensed-fixture","config_hash":"c"*64,
            "settings_version":1,"data_snapshot_hash":"d"*64,"matching_ready":True,
            "quality":"healthy",**extra}


def test_golden_buy_mark_sell_and_restart(tmp_path):
    path=tmp_path/"golden.db";repo=SQLiteRepository(path);repo.ensure_simulation_account(100_000)
    repo.append_simulation_intents("buy-run","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",
                                   [{"symbol":"TEST","side":"buy","amount":20_000,"target_weight":.2,"initial_weight":.2}])
    outcome=repo.match_pending("2026-07-07T18:00:00+08:00",{"TEST":bar()})
    assert outcome==[{"id":1,"symbol":"TEST","side":"buy","status":"filled","filled_quantity":1900,"reason":"ok"}]
    position=repo.simulation_positions()["TEST"]
    assert position["shares"]==1900
    valuation=repo.mark_to_market("2026-07-07",{"TEST":11},lineage(fixture="golden"))
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
    equity=restarted.simulation()["daily_equity"][0]
    assert equity["model_version"]==MODEL_VERSION and equity["config_hash"]=="c"*64
    assert equity["settings_version"]==1 and equity["data_snapshot_hash"]=="d"*64


def test_same_day_different_lineages_are_append_only(tmp_path):
    repo=SQLiteRepository(tmp_path/"lineage.db");repo.ensure_simulation_account(100_000)
    repo.mark_to_market("2026-07-07",{},lineage(config_hash="a"*64))
    repo.mark_to_market("2026-07-07",{},lineage(config_hash="b"*64,settings_version=2))
    rows=repo.simulation()["daily_equity"]
    assert len(rows)==2
    assert {row["config_hash"] for row in rows}=={"a"*64,"b"*64}


def test_deployed_legacy_equity_table_migrates_without_claiming_lineage(tmp_path):
    path=tmp_path/"legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE simulation_equity(trade_date TEXT PRIMARY KEY,recorded_at TEXT NOT NULL,cash REAL NOT NULL,market_value REAL NOT NULL,equity REAL NOT NULL,drawdown REAL NOT NULL,payload TEXT NOT NULL)")
        db.execute("INSERT INTO simulation_equity VALUES(?,?,?,?,?,?,?)",
                   ("2026-07-01","2026-07-01T18:00:00+08:00",100000,0,100000,0,"{}"))
    repo=SQLiteRepository(path);repo.ensure_simulation_account(100_000)
    with repo.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM simulation_equity_legacy").fetchone()[0]==1
        columns={row["name"] for row in db.execute("PRAGMA table_info(simulation_equity)")}
    assert {"config_hash","settings_version","data_snapshot_hash","recorded_at"}<=columns
    assert repo.simulation()["daily_equity"]==[]


def test_split_and_dividend_adjust_account_without_adj_factor_shortcut(tmp_path):
    repo=SQLiteRepository(tmp_path/"actions.db");repo.ensure_simulation_account(100_000)
    repo.append_simulation_intents("seed","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",
                                   [{"symbol":"TEST","side":"buy","quantity":1000}])
    repo.match_pending("2026-07-07T18:00:00+08:00",{"TEST":bar(open_=10,close=10)})
    before=repo.simulation_positions()["TEST"]
    action_bar=bar(open_=5,close=5,share_multiplier=2.0,cash_dividend_per_share=.1,adj_factor=99)
    applied=repo.apply_corporate_actions("action-run",{"TEST":action_bar})
    after=repo.simulation_positions()["TEST"]
    assert applied[0]["old_shares"]==1000 and after["shares"]==2000
    assert after["avg_cost"]==pytest.approx(before["avg_cost"]/2)
    assert repo.simulation()["account"]["cash"]==pytest.approx(100_000-before["shares"]*10.008-
                                                               max(5,before["shares"]*10.008*.0003)+100)
    assert repo.apply_corporate_actions("action-replay",{"TEST":action_bar})==[]


def test_terminal_write_down_is_audited_and_removes_stale_valuation(tmp_path):
    repo=SQLiteRepository(tmp_path/"terminal.db");repo.ensure_simulation_account(100_000)
    repo.append_simulation_intents("seed","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",
                                   [{"symbol":"TEST","side":"buy","quantity":1000}])
    repo.match_pending("2026-07-07T18:00:00+08:00",{"TEST":bar(open_=10,close=10)})
    outcome=repo.write_down_terminal_positions("terminal-run",{"TEST":"provider_confirmed_delisted"})
    assert outcome[0]["status"]=="written_down" and repo.simulation_positions()=={}
    valuation=repo.mark_to_market("2026-07-08",{},lineage(data_snapshot_hash="e"*64))
    assert valuation["market_value"]==0 and valuation["cash"]==valuation["equity"]
    event=next(item for item in repo.simulation()["ledger"] if item["event_type"]=="forced_write_down")
    assert event["status"]=="written_down" and event["payload"]["recovery_price"]==0


def test_production_simulation_uses_initial_then_confirmed_target(tmp_path):
    service=QuantService(provider=DeterministicDemoProvider(),repository=SQLiteRepository(tmp_path/"stages.db"))
    initial=service.run_eod(date(2026,7,3))
    assert {item["action"] for item in initial["portfolio"]}=={"待买"}
    assert {item["stage"] for item in initial["simulation"]["new_intents"]}=={"initial"}
    assert all(item["execution_target_weight"]==item["initial_weight"]
               for item in initial["portfolio"])

    confirmed=service.run_eod(date(2026,7,6))
    assert confirmed["simulation"]["matched"]
    assert {item["action"] for item in confirmed["portfolio"]}=={"加仓"}
    assert {item["stage"] for item in confirmed["simulation"]["new_intents"]}=={"add_confirmation"}
    assert all(item["execution_target_weight"]==item["target_weight"]
               for item in confirmed["portfolio"])


def test_production_plan_emits_target_reduction_and_real_current_weight():
    portfolio=[{"symbol":"TEST","target_weight":.10,"initial_weight":.06,"action":"持有",
                "current_weight":0.0}]
    latest={"TEST":bar(open_=10,close=10)}
    payloads,intents,state=QuantService._simulation_execution_plan(
        portfolio,[],latest,{"TEST":{"shares":2000,"avg_cost":8}},100_000,
    )
    assert payloads[0]["current_weight"]==.20 and payloads[0]["action"]=="减仓"
    assert intents==[{"symbol":"TEST","target_weight":.1,"initial_weight":.06,
                      "current_weight":.2,"execution_target_weight":.1,
                      "model_action":"减仓","stage":"target_reduction","side":"sell","quantity":1000}]
    assert state["basis"]=="actual_simulated_shares_vs_model_execution_target"


def test_partial_and_rejected_constraints_are_persisted(tmp_path):
    repo=SQLiteRepository(tmp_path/"constraints.db");repo.ensure_simulation_account(100_000)
    repo.append_simulation_intents("partial","2026-07-06T18:00:00+08:00","2026-07-07T09:30:00+08:00",
                                   [{"symbol":"LOWVOL","side":"buy","amount":50_000}])
    partial=repo.match_pending("2026-07-07T18:00:00+08:00",{"LOWVOL":bar("LOWVOL",volume=10_000)})[0]
    assert partial["status"]=="partial" and partial["filled_quantity"]==100
    repo.append_simulation_intents("rejected","2026-07-07T18:00:00+08:00","2026-07-08T09:30:00+08:00",
                                   [{"symbol":"HALT","side":"buy","amount":20_000}])
    outcomes=repo.match_pending("2026-07-08T18:00:00+08:00",{"HALT":bar("HALT",suspended=True)})
    rejected=next(item for item in outcomes if item["symbol"]=="HALT")
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
