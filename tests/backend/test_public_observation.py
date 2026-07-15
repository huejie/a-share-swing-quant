from datetime import date, datetime
from zoneinfo import ZoneInfo

from quant_system.providers import DeterministicDemoProvider
from quant_system.repository import SQLiteRepository
from quant_system.service import QuantService, Settings


class PublicObservationFixture(DeterministicDemoProvider):
    name = "tushare"

    def __init__(self, adjustment_status: str = "available"):
        self.adjustment_status = adjustment_status

    def status(self):
        return {
            "provider": self.name,
            "available": True,
            "observation_only": True,
            "production_ready": False,
            "pit_verified": False,
        }

    def load(self, as_of=None):
        snapshot = super().load(as_of)
        snapshot.metadata.update({
            "public_data": True,
            "observation_only": True,
            "production_ready": False,
            "pit_verified": False,
            "research_eligible": False,
            "enrichments": {
                "adj_factor": {"status": self.adjustment_status},
                "daily_basic": {"status": "available"},
                "index_daily": {"status": "available"},
            },
        })
        return snapshot


def test_complete_public_data_can_drive_forward_observation_but_not_production(tmp_path):
    service = QuantService(
        provider=PublicObservationFixture(),
        repository=SQLiteRepository(tmp_path / "observation.db"),
    )
    result = service.run_eod(date.today(), run_key="public-observation-complete")

    assert result["displayable"] is True
    assert result["published"] is False
    assert result["production_published"] is False
    assert result["release_mode"] == "observation_only"
    assert result["portfolio_status"] == "observation"
    assert result["portfolio_condition"] in {"healthy", "partial", "risk_off"}
    assert result["quality"]["status"] == "observation_only"
    assert result["research_eligible"] is False
    assert result["data_provenance"]["public_data"] is True
    assert result["data_provenance"]["production_ready"] is False
    assert result["simulation"]["broker_connected"] is False
    assert result["simulation"]["matching_ready"] is False
    assert result["simulation"]["new_intents"] == []
    assert service.repository.list_decisions(1)[0]["snapshot"]["release_mode"] == "observation_only"
    active_status = next(item for item in service.provider_statuses() if item["provider"] == "tushare")
    assert active_status["available"] is True
    assert active_status["observation_only"] is True

    replay = service.run_eod(date.today(), run_key="public-observation-complete")
    assert replay["idempotent_replay"] is True
    assert replay["decision_id"] == result["decision_id"]


def test_observation_forces_research_ineligible_even_if_provider_metadata_is_wrong(tmp_path):
    class MislabelledPublicFixture(PublicObservationFixture):
        def load(self, as_of=None):
            snapshot=super().load(as_of)
            snapshot.metadata["research_eligible"]=True
            return snapshot

    service=QuantService(
        provider=MislabelledPublicFixture(),
        repository=SQLiteRepository(tmp_path / "mislabelled-observation.db"),
    )
    result=service.run_eod(date.today(),run_key="public-observation-mislabelled")

    assert result["release_mode"]=="observation_only"
    assert result["published"] is False and result["displayable"] is True
    assert result["research_eligible"] is False
    decision=service.repository.list_decisions(1)[0]
    assert decision["research_eligible"] is False
    assert decision["snapshot"]["research_eligible"] is False


def test_public_observation_still_fails_closed_on_required_adjustment_error(tmp_path):
    service = QuantService(
        provider=PublicObservationFixture("partial"),
        repository=SQLiteRepository(tmp_path / "blocked-observation.db"),
    )
    result = service.run_eod(date.today(), run_key="public-observation-bad-adjustment")

    assert result["published"] is False
    assert result.get("displayable") is not True
    assert result["quality"]["status"] == "blocked"
    codes = {issue["code"] for issue in result["quality"]["issues"]}
    assert "ENRICHMENT_REQUIRED:adj_factor" in codes
    assert service.repository.list_decisions() == []


def test_configured_board_scope_is_applied_to_data_and_recorded(tmp_path):
    settings = Settings(include_main=False, include_chinext=True, include_star=False, include_bse=False)
    service = QuantService(
        settings=settings,
        repository=SQLiteRepository(tmp_path / "chinext-only.db"),
    )
    result = service.run_eod(date(2026, 7, 3), run_key="chinext-scope")

    assert result["published"] is True
    assert service.snapshot is not None
    assert {bar.board for bar in service.snapshot.bars} == {"创业板"}
    scoped_symbols={bar.symbol for bar in service.snapshot.bars}
    assert {item["symbol"] for item in result["candidates"]}<=scoped_symbols
    assert {item["symbol"] for item in result["portfolio"]}<=scoped_symbols
    scope = service.snapshot.metadata["universe_scope"]
    assert scope == {
        "include_main": False,
        "include_chinext": True,
        "include_star": False,
        "include_bse": False,
        "symbols_before": 12,
        "symbols_after": 3,
    }


def test_declared_exchange_calendar_day_takes_priority_over_weekday_guess():
    friday = datetime(2026, 10, 2, 18, tzinfo=ZoneInfo("Asia/Shanghai"))

    # Monday is deliberately skipped to represent a statutory market holiday.
    effective = QuantService._next_trading_time(friday, "2026-10-06")

    assert effective.isoformat() == "2026-10-06T09:30:00+08:00"


def test_excluded_board_holding_keeps_full_market_valuation_and_can_be_sold(tmp_path):
    provider=DeterministicDemoProvider()
    repo=SQLiteRepository(tmp_path / "board-switch.db")
    repo.ensure_simulation_account(1_000_000)
    star_symbol="688981.SH"
    seed_snapshot=provider.load(date(2026,7,2))
    seed_bar=next(bar for bar in seed_snapshot.bars if bar.symbol==star_symbol and bar.day==date(2026,7,2))
    repo.append_simulation_intents(
        "seed-star","2026-07-01T18:00:00+08:00","2026-07-02T09:30:00+08:00",
        [{"symbol":star_symbol,"side":"buy","amount":100_000}],
    )
    assert repo.match_pending(seed_snapshot.as_of.isoformat(),{star_symbol:seed_bar})[0]["status"]=="filled"

    service=QuantService(
        provider=provider,
        settings=Settings(include_main=True,include_chinext=False,include_star=False,include_bse=False),
        repository=repo,
    )
    excluded=service.run_eod(date(2026,7,3),run_key="exclude-star-board")

    assert star_symbol not in {bar.symbol for bar in service.snapshot.bars}
    assert excluded["simulation"]["valuation"]["market_value"]>0
    assert any(intent["symbol"]==star_symbol and intent["side"]=="sell" for intent in excluded["simulation"]["new_intents"])

    settled=service.run_eod(date(2026,7,6),run_key="settle-excluded-star")
    outcome=next(item for item in settled["simulation"]["matched"] if item["symbol"]==star_symbol)
    assert outcome["status"] in {"filled","partial"}
    assert outcome["reason"] in {"ok","volume_or_cash_limited"}


def test_empty_board_scope_fails_closed_without_entering_scoring(tmp_path):
    service=QuantService(
        settings=Settings(include_main=False,include_chinext=False,include_star=False,include_bse=False),
        repository=SQLiteRepository(tmp_path / "empty-board-scope.db"),
    )

    result=service.run_eod(date(2026,7,3),run_key="empty-board-scope")

    assert result["published"] is False
    assert result.get("displayable") is not True
    assert result["quality"]["status"]=="blocked"
    assert {issue["code"] for issue in result["quality"]["issues"]}>={"NO_DATA"}


def test_restart_serves_persisted_dashboard_without_reloading_public_provider(tmp_path):
    database=tmp_path / "restart-cache.db"
    first=QuantService(provider=PublicObservationFixture(),repository=SQLiteRepository(database))
    expected=first.run_eod(date.today())

    class MustNotReload(PublicObservationFixture):
        def load(self, as_of=None):
            raise AssertionError("persisted dashboard must not reload the public provider")

    restarted=QuantService(provider=MustNotReload(),repository=SQLiteRepository(database))
    actual=restarted.ensure()

    assert actual["decision_id"]==expected["decision_id"]
    assert restarted.snapshot is None
