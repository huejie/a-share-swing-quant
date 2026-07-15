"""Completion-level tests for explicit product requirements.

These cases intentionally test requirements that are easy to miss when the
individual scoring helpers are tested in isolation.  They are acceptance
tests, not implementation-shape tests: a different implementation is valid as
long as the externally observable invariants hold.
"""

from dataclasses import replace
from datetime import date, timedelta

from fastapi.testclient import TestClient

import apps.api.main as api_main
from quant_system.engine import assess_market, assess_stocks, assess_themes, build_portfolio, entry_signal
from quant_system.models import Bar, Lifecycle
from quant_system.providers import DeterministicDemoProvider
from quant_system.repository import SQLiteRepository
from quant_system.service import QuantService


def test_weak_market_portfolio_preserves_documented_15_percent_single_name_floor():
    """PRT-005: reducing exposure must reduce count/cash, not create tiny names."""
    snapshot = DeterministicDemoProvider().load()
    market = replace(assess_market(snapshot), exposure_cap=0.45)
    stocks = assess_stocks(snapshot, assess_themes(snapshot))
    portfolio = build_portfolio(
        snapshot,
        market,
        stocks,
        target_count=4,
        # This case isolates the sizing invariant from the independent entry
        # signal gate.
        allow_low_score_symbols={stock.symbol for stock in stocks},
    )

    assert 3 <= len(portfolio) <= 5
    assert all(0.15 <= position.target_weight <= 0.25 for position in portfolio)
    assert sum(position.target_weight for position in portfolio) <= 0.45


def test_new_positions_only_use_documented_entry_lifecycles():
    """THM-002: acceleration/crowding/fading are not valid new-entry phases."""
    snapshot = DeterministicDemoProvider().load()
    themes = assess_themes(snapshot)
    lifecycle_by_theme = {theme.name: theme.lifecycle for theme in themes}
    portfolio = build_portfolio(
        snapshot,
        assess_market(snapshot),
        assess_stocks(snapshot, themes),
    )

    assert portfolio
    assert all(
        lifecycle_by_theme[position.theme] in {
            Lifecycle.STARTING,
            Lifecycle.EXPANDING,
            Lifecycle.HEALTHY,
        }
        for position in portfolio
        if position.action == "待买"
    )


def test_downtrend_without_pullback_confirmation_is_not_a_buy_signal():
    """SIG-002: failure to break out must not automatically mean trend pullback."""
    start = date(2026, 1, 1)
    bars = []
    for index in range(80):
        close = 20 - index * 0.1
        bars.append(
            Bar(
                "TEST.SH", "测试", start + timedelta(days=index), close + 0.1,
                close + 0.2, close - 0.2, close, 1_000_000 + index * 10_000,
                close * 1_000_000, "测试题材", "测试行业",
            )
        )

    signal = entry_signal(bars)

    assert signal is None or signal[0] != "趋势回踩"


def test_candidate_pool_is_two_or_three_names_and_disjoint_from_portfolio(tmp_path):
    """PRT-002: candidates are a separate 2--3 name pool, not a ranked duplicate."""
    service = QuantService(repository=SQLiteRepository(tmp_path / "candidates.db"))
    result = service.run_eod(run_key="completion-candidate-pool")
    portfolio = {item["symbol"] for item in result["portfolio"]}
    candidates = {item["symbol"] for item in result["candidates"]}

    assert 2 <= len(candidates) <= 3
    assert portfolio.isdisjoint(candidates)


def test_user_settings_survive_service_restart(tmp_path, monkeypatch):
    """Settings advertised as saved must not revert when the API process restarts."""
    database = tmp_path / "settings.db"
    first = QuantService(repository=SQLiteRepository(database))
    monkeypatch.setattr(api_main, "service", first)
    client = TestClient(api_main.app)

    response = client.patch(
        "/api/v1/settings",
        json={"capital": 500_000, "target_count": 3, "max_portfolio_drawdown": 0.15},
    )
    assert response.status_code == 200

    restarted = QuantService(repository=SQLiteRepository(database))
    assert restarted.settings.capital == 500_000
    assert restarted.settings.target_count == 3
    assert restarted.settings.max_portfolio_drawdown == 0.15


def test_capital_change_does_not_leave_simulation_on_previous_account_size(tmp_path):
    """PRT-001/SIM-002: sizing and the persisted simulated account must agree."""
    service = QuantService(repository=SQLiteRepository(tmp_path / "capital-change.db"))
    service.run_eod(run_key="capital-before-change")
    service.update_settings({"capital": 500_000})
    service.run_eod(run_key="capital-after-change")

    account = service.simulation()["account"]
    assert account["initial_capital"] == 500_000


def test_openapi_describes_response_contracts_used_by_the_frontend():
    """NFR-002: versioned APIs need machine-checkable response schemas."""
    schema = api_main.app.openapi()
    operations = (
        ("/api/v1/dashboard", "get", {"as_of", "provider", "market", "portfolio", "candidates", "cash_weight"}),
        ("/api/v1/portfolio", "get", {"positions", "cash_weight", "model_portfolio_only"}),
        ("/api/v1/data/status", "get", {"quality", "providers", "active", "provenance"}),
        ("/api/v1/simulation", "get", {"simulated_account", "simulated_positions", "ledger", "daily_equity"}),
        ("/api/v1/settings", "get", {"capital", "target_count", "max_portfolio_drawdown"}),
        ("/api/v1/settings", "patch", {"capital", "target_count", "max_portfolio_drawdown"}),
    )
    for path, method, required_fields in operations:
        response_schema = schema["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema, f"{method.upper()} {path} has an empty OpenAPI response schema"
        if "$ref" in response_schema:
            response_schema = schema["components"]["schemas"][response_schema["$ref"].rsplit("/", 1)[-1]]
        properties = set(response_schema.get("properties", {}))
        assert required_fields <= properties, (
            f"{method.upper()} {path} does not describe required response fields; "
            f"missing {sorted(required_fields - properties)}"
        )


def test_mutating_and_resource_intensive_routes_declare_authentication():
    """Architecture section 7: public reads may be open; mutations/jobs may not."""
    schema = api_main.app.openapi()
    protected_operations = (
        ("/api/v1/pipeline/eod", "post"),
        ("/api/v1/backtests", "post"),
        ("/api/v1/research/runs", "post"),
        ("/api/v1/settings", "patch"),
    )
    assert schema.get("components", {}).get("securitySchemes")
    for path, method in protected_operations:
        assert schema["paths"][path][method].get("security"), f"{method.upper()} {path} is unauthenticated"


def test_configured_admin_key_is_enforced_at_runtime(tmp_path, monkeypatch):
    """Deployment auth must fail closed, not exist only as OpenAPI metadata."""
    key = "a" * 64
    monkeypatch.setenv("QUANT_ADMIN_API_KEY", key)
    monkeypatch.setattr(
        api_main,
        "service",
        QuantService(repository=SQLiteRepository(tmp_path / "authenticated-settings.db")),
    )
    client = TestClient(api_main.app)

    assert client.patch("/api/v1/settings", json={"capital": 500_000}).status_code == 401
    assert client.patch(
        "/api/v1/settings",
        headers={"X-Admin-Key": "wrong"},
        json={"capital": 500_000},
    ).status_code == 401
    accepted = client.patch(
        "/api/v1/settings",
        headers={"X-Admin-Key": key},
        json={"capital": 500_000},
    )
    assert accepted.status_code == 200
    assert accepted.json()["capital"] == 500_000
