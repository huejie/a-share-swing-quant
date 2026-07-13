from datetime import date, datetime, timedelta, timezone
import json

import pytest

from quant_system.backtest import BacktestResult
from quant_system.models import DataSnapshot
from quant_system.research import (
    CAPITAL_TIERS, CORE_FACTORS, GateInput, canonical_json, content_hash,
    evaluate_baselines, evaluate_capacity, evaluate_gates, factor_ablation,
    make_manifest, make_time_series_split, parameter_sensitivity,
    stress_scenarios, write_evidence_package,
)


def fake_result(capital=1_000_000, rebalance_days=10, slippage_bps=8, **_):
    penalty = abs(rebalance_days - 10) * .002 + abs(slippage_bps - 8) * .0002
    return BacktestResult(
        capital, capital * (1.12 - penalty), .12 - penalty, .08, -.12 - penalty,
        .15, 0.9 - penalty, [], [{"date": "2025-01-01", "equity": capital}],
        {"lot_size": 100, "max_daily_amount_participation": .01},
    )


@pytest.fixture
def snapshot():
    return DataSnapshot(datetime(2026, 7, 6, tzinfo=timezone.utc), [], "deterministic-demo", 0)


def test_chronological_split_is_non_overlapping_and_final_test_locked():
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(100)]
    split = make_time_series_split(days, embargo_observations=2)
    train, validation, test = split.windows
    assert train.end < validation.start < validation.end < test.start
    assert train.may_tune and validation.may_tune and not test.may_tune
    assert split.final_test_isolated and "禁止参与参数选择" in split.declaration
    assert split.dates_hash == content_hash([d.isoformat() for d in days])


def test_split_rejects_too_little_data_and_invalid_ratios():
    with pytest.raises(ValueError):
        make_time_series_split([date(2020, 1, 1)] * 20)
    with pytest.raises(ValueError):
        make_time_series_split([date(2020, 1, 1) + timedelta(days=i) for i in range(20)], .9, .2)


def test_three_mandatory_baselines_have_metrics_and_provenance():
    returns = {
        "沪深300": [.01, -.02, .03], "中证全指": [.02, -.01, .01], "简单动量": [.03, -.03, .02],
    }
    results = evaluate_baselines(returns, {name: "licensed-fixture" for name in returns},
                                 {name: True for name in returns})
    assert [x.name for x in results] == list(returns)
    assert all(x.observations == 3 and x.is_official_point_in_time for x in results)
    with pytest.raises(ValueError, match="missing required"):
        evaluate_baselines({"沪深300": [.01]})


def test_four_exact_capital_tiers_and_capacity_constraints(snapshot):
    results = evaluate_capacity(snapshot, runner=lambda snap, **kwargs: fake_result(**kwargs))
    assert tuple(x.capital for x in results) == CAPITAL_TIERS
    assert all(x.executable and x.lot_size == 100 and x.max_participation == .01 for x in results)
    with pytest.raises(ValueError, match="exactly"):
        evaluate_capacity(snapshot, (100_000,), runner=lambda snap, **kwargs: fake_result(**kwargs))


def test_parameter_sensitivity_marks_neighbor_stability(snapshot):
    results = parameter_sensitivity(snapshot, runner=lambda snap, **kwargs: fake_result(**kwargs))
    assert len(results) == 9
    assert any(x.parameters == {"rebalance_days": 10, "slippage_bps": 8.0} for x in results)
    assert sum(x.stable_neighbor for x in results) >= 6


def test_ablation_removes_each_core_factor_once():
    calls = []
    def evaluator(active):
        calls.append(active)
        return len(active) / 10
    results = factor_ablation(evaluator, evaluator_label="shared-engine-fixture")
    assert tuple(x.removed_factor for x in results) == CORE_FACTORS
    assert all(x.delta_from_full == -.1 for x in results)
    assert len(calls) == 1 + len(CORE_FACTORS)


def test_stress_scenarios_are_deterministic_and_include_tail_event():
    a = stress_scenarios([.01] * 12); b = stress_scenarios([.01] * 12)
    assert a == b and len(a) == 3
    gap = next(x for x in a if x.name == "单日跳空冲击")
    assert gap.max_drawdown <= -.09


def credible_metrics(**overrides):
    values = dict(oos_max_drawdown=-.13, average_holdings=4, median_holding_days=57,
                  after_cost_excess_return=.06, max_year_contribution=.35,
                  max_theme_contribution=.40, max_stock_contribution=.20,
                  neighbor_stability_ratio=.78, baseline_count=3, capacity_tier_count=4,
                  final_test_isolated=True, point_in_time_verified=True,
                  production_data_authorized=True, simulation_observation_weeks=9)
    values.update(overrides)
    return GateInput(**values)


def test_gates_pass_only_when_every_requirement_has_evidence():
    passed = evaluate_gates(credible_metrics())
    assert passed["overall"] == "PASS"
    failed = evaluate_gates(credible_metrics(oos_max_drawdown=-.19))
    assert failed["overall"] == "FAIL"
    statuses = {x["id"]: x["status"] for x in failed["gates"]}
    assert statuses["BT-008-DRAWDOWN-HARD"] == "FAIL"


def test_demo_cannot_pass_pit_license_or_observation_gate():
    result = evaluate_gates(credible_metrics(point_in_time_verified=False,
                                             production_data_authorized=False,
                                             simulation_observation_weeks=0))
    statuses = {x["id"]: x for x in result["gates"]}
    assert result["overall"] == "FAIL"
    assert result["candidate_label"] == "工程候选版/模拟观察中"
    assert statuses["DAT-PIT"]["status"] == "FAIL" and "Demo" in statuses["DAT-PIT"]["note"]
    assert statuses["SIM-003-OBSERVATION"]["status"] == "FAIL"


def test_manifest_hash_and_immutable_evidence_package(tmp_path):
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(30)]
    split = make_time_series_split(days)
    generated = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    manifest = make_manifest(git_commit="abc123", model_version="m1", feature_version="f1",
                             data_version="demo-v1", config={"seed": 7}, random_seed=7,
                             split=split, provider="deterministic-demo", generated_at=generated)
    same = make_manifest(git_commit="abc123", model_version="m1", feature_version="f1",
                         data_version="demo-v1", config={"seed": 7}, random_seed=7,
                         split=split, provider="deterministic-demo", generated_at=generated)
    assert manifest == same and manifest["manifest_hash"]
    assert any("8至12周" in x for x in manifest["limitations"])
    gates = evaluate_gates(credible_metrics(point_in_time_verified=False, simulation_observation_weeks=0))
    hashes = write_evidence_package(tmp_path / "run-1", manifest, gates, {"scope": "demo"})
    assert set(hashes) == {"manifest.json", "gates.json", "research_report.json", "hashes.json"}
    assert json.loads((tmp_path / "run-1" / "gates.json").read_text("utf-8"))["overall"] == "FAIL"
    with pytest.raises(FileExistsError):
        write_evidence_package(tmp_path / "run-1", manifest, gates, {})


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    assert content_hash({"b": 2, "a": 1}) == content_hash({"a": 1, "b": 2})
