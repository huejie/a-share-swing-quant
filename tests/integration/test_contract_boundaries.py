"""Independent QA coverage for high-risk product invariants.

This suite intentionally lives outside ``tests/backend`` so it can be run as an
acceptance layer without overwriting the implementation team's tests.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from apps.api.main import app, service
from quant_system.engine import (
    assess_market,
    assess_stocks,
    build_portfolio,
    trailing_stop,
)
from quant_system.providers import DeterministicDemoProvider
from quant_system.quality import check_quality
from quant_system.research import GateInput, evaluate_gates


client = TestClient(app)


def test_trailing_stop_exact_activation_and_formula():
    assert trailing_stop(100, 114.99) is None
    assert trailing_stop(100, 115) == 110.5
    assert trailing_stop(100, 130) == 121


def test_portfolio_constraints_for_supported_capital_tiers():
    snapshot = DeterministicDemoProvider().load()
    market = assess_market(snapshot)
    for capital in (100_000, 1_000_000, 3_000_000, 10_000_000):
        stocks = assess_stocks(snapshot, capital=capital)
        portfolio = build_portfolio(snapshot, market, stocks, capital=capital)
        assert 3 <= len(portfolio) <= 5
        assert all(position.target_weight <= 0.25 for position in portfolio)
        assert sum(position.target_weight for position in portfolio) <= market.exposure_cap
        assert len({position.theme for position in portfolio}) >= 2
        assert all(
            sum(other.theme == position.theme for other in portfolio) <= 2
            for position in portfolio
        )


def test_three_positions_retain_at_least_25_percent_cash():
    snapshot = DeterministicDemoProvider().load()
    market = assess_market(snapshot)
    portfolio = build_portfolio(
        snapshot,
        market,
        assess_stocks(snapshot),
        target_count=3,
    )
    assert len(portfolio) == 3
    assert sum(position.target_weight for position in portfolio) <= 0.75


def test_stale_quality_is_blocking_and_pipeline_can_suppress_publication():
    snapshot = DeterministicDemoProvider().load(date(2025, 1, 3))
    report = check_quality(
        snapshot,
        datetime(2026, 7, 6, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert report.status == "blocked"
    response = client.post(
        "/api/v1/pipeline/eod",
        json={"as_of": "2025-01-03", "enforce_freshness": True},
    )
    assert response.status_code == 200
    assert response.json()["published"] is False
    service.latest = None


def test_no_real_trading_routes_or_openapi_language():
    schema = client.get("/openapi.json").json()
    paths = " ".join(schema["paths"]).lower()
    assert not any(term in paths for term in ("/broker", "/orders", "/execute"))
    assert "自动交易" in schema["info"]["description"]


def test_demo_evidence_cannot_pass_release_gate_or_time_requirement():
    gates = evaluate_gates(
        GateInput(
            oos_max_drawdown=-0.10,
            average_holdings=4,
            median_holding_days=60,
            after_cost_excess_return=0.10,
            max_year_contribution=0.30,
            max_theme_contribution=0.30,
            max_stock_contribution=0.30,
            neighbor_stability_ratio=0.8,
            baseline_count=3,
            capacity_tier_count=4,
            capacity_executable=(True, True, True, True),
            walk_forward_oos_complete=True,
            stress_evidence_complete=True,
            final_test_isolated=True,
            point_in_time_verified=False,
            production_data_authorized=False,
            simulation_observation_weeks=0,
        )
    )
    failed = {gate["id"] for gate in gates["gates"] if gate["status"] == "FAIL"}
    assert gates["overall"] == "FAIL"
    assert gates["candidate_label"] == "工程候选版/模拟观察中"
    assert {"DAT-PIT", "DATA-LICENSE", "SIM-003-OBSERVATION"} <= failed
