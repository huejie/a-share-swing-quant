from datetime import date
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from quant_system.repository import SCHEMA_VERSION, SQLiteRepository
from quant_system.service import QuantService
from quant_system.providers import DeterministicDemoProvider
import quant_system.service as service_module


def test_schema_version_is_recorded_for_fresh_database(tmp_path):
    repository = SQLiteRepository(tmp_path / "fresh-schema.db")
    assert repository.schema_version() == SCHEMA_VERSION


def test_legacy_simulation_schema_is_upgraded_and_versioned(tmp_path):
    path = tmp_path / "legacy-schema.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE simulation_equity(
          account_id TEXT NOT NULL, trade_date TEXT NOT NULL, cash REAL NOT NULL,
          market_value REAL NOT NULL, equity REAL NOT NULL, drawdown REAL NOT NULL,
          PRIMARY KEY(account_id, trade_date)
        );
        CREATE TABLE simulation_positions(
          account_id TEXT NOT NULL, symbol TEXT NOT NULL, shares INTEGER NOT NULL,
          avg_cost REAL NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(account_id, symbol)
        );
        """)
    repository = SQLiteRepository(path)
    assert repository.schema_version() == SCHEMA_VERSION
    with repository.connect() as db:
        equity_columns = {row["name"] for row in db.execute("PRAGMA table_info(simulation_equity)")}
        position_columns = {row["name"] for row in db.execute("PRAGMA table_info(simulation_positions)")}
        assert "id" in equity_columns and "recorded_at" in equity_columns
        assert {"last_price", "last_price_at"}.issubset(position_columns)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='simulation_equity_legacy'").fetchone()


class LicensedFixtureProvider(DeterministicDemoProvider):
    name="licensed-fixture"


def test_sqlite_persists_across_service_restart(tmp_path):
    path=tmp_path/"product.db";repo1=SQLiteRepository(path);one=QuantService(repository=repo1)
    result=one.run_eod(date(2026,7,3),run_key="restart-proof-run")
    ident,backtest=one.run_backtest(100_000)
    repo2=SQLiteRepository(path);two=QuantService(repository=repo2)
    assert two.decisions[0]["id"]==result["decision_id"]
    assert repo2.get_backtest(ident)["final_equity"]==backtest["final_equity"]
    sim=repo2.simulation()
    assert sim["ledger"] and sim["daily_equity"]
    stored=repo2.get_input_snapshot(result["data_snapshot_hash"])
    assert stored["provider"]=="deterministic-demo" and stored["bars"]
    assert repo2.input_snapshot_status(result["data_snapshot_hash"])["compressed_bytes"]>0


def test_same_run_key_is_append_idempotent(tmp_path):
    service=QuantService(repository=SQLiteRepository(tmp_path/"idem.db"))
    a=service.run_eod(run_key="same-key-proof")
    count=len(service.simulation()["ledger"])
    b=service.run_eod(run_key="same-key-proof")
    assert a["decision_id"]==b["decision_id"] and len(service.simulation()["ledger"])==count


def test_non_demo_provider_enforces_freshness_by_default(tmp_path):
    service=QuantService(provider=LicensedFixtureProvider(),repository=SQLiteRepository(tmp_path/"fresh.db"))
    result=service.run_eod(date(2025,1,3),run_key="licensed-stale-default")
    assert result["published"] is False and result["quality"]["freshness"]=="stale"
    assert service.repository.list_decisions()==[]


def test_concurrent_same_run_key_returns_one_decision(tmp_path):
    service=QuantService(repository=SQLiteRepository(tmp_path/"concurrent.db"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:service.run_eod(run_key="concurrent-proof"),range(3)))
    assert len({x["decision_id"] for x in results})==1
    assert len([x for x in service.simulation()["ledger"] if x["run_key"]=="concurrent-proof"])<=5


def test_non_demo_default_run_key_changes_with_calendar_day(monkeypatch,tmp_path):
    class MutableDate(date):
        current=date(2026,7,13)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(service_module,"date",MutableDate)
    service=QuantService(
        provider=LicensedFixtureProvider(),
        repository=SQLiteRepository(tmp_path / "daily-default-key.db"),
    )

    first=service.run_eod()
    MutableDate.current=date(2026,7,14)
    second=service.run_eod()

    assert first["run_key"]!=second["run_key"]
    assert first["as_of"].startswith("2026-07-13")
    assert second["as_of"].startswith("2026-07-14")
    assert second.get("idempotent_replay") is not True


def test_automatic_run_key_tracks_risk_config_and_data_snapshot(tmp_path):
    class RevisableProvider(DeterministicDemoProvider):
        revision=0.0
        def load(self,as_of=None):
            snapshot=super().load(as_of)
            if self.revision:
                snapshot.bars[-1]=replace(snapshot.bars[-1],close=snapshot.bars[-1].close+self.revision,
                                          high=max(snapshot.bars[-1].high,snapshot.bars[-1].close+self.revision))
            return snapshot

    provider=RevisableProvider()
    service=QuantService(provider=provider,repository=SQLiteRepository(tmp_path/"lineage.db"))
    first=service.run_eod(date(2026,7,3))
    assert len(first["config_hash"])==64 and len(first["data_snapshot_hash"])==64

    service.settings.max_portfolio_drawdown=.12
    config_changed=service.run_eod(date(2026,7,3))
    assert config_changed["run_key"]!=first["run_key"]

    provider.revision=.01
    data_changed=service.run_eod(date(2026,7,3))
    assert data_changed["run_key"]!=config_changed["run_key"]
    assert data_changed["data_snapshot_hash"]!=config_changed["data_snapshot_hash"]
