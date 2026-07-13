from datetime import date
from concurrent.futures import ThreadPoolExecutor
from quant_system.repository import SQLiteRepository
from quant_system.service import QuantService
from quant_system.providers import DeterministicDemoProvider
import quant_system.service as service_module


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
