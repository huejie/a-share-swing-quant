from datetime import date, datetime, timedelta, timezone
import json

import pytest

from quant_system.backtest import BacktestResult, Fill
from quant_system.models import Bar, DataSnapshot
from quant_system.research import (
    CAPITAL_TIERS, CORE_FACTORS, GateInput, canonical_json, content_hash,
    evaluate_baseline_contract, evaluate_baselines, evaluate_capacity,
    evaluate_contribution_attribution, evaluate_gates, evaluate_trade_statistics,
    factor_ablation, make_manifest,
    make_parameter_selection_snapshot, make_time_series_split, parameter_sensitivity,
    stress_scenarios, walk_forward_oos, write_evidence_package,
)


def fake_result(capital=1_000_000, rebalance_days=10, slippage_bps=8, **_):
    penalty = abs(rebalance_days - 10) * .002 + abs(slippage_bps - 8) * .0002
    fill = Fill(date(2025, 1, 1), date(2025, 1, 2), "000001.SZ", "buy", 10, 1000, 5)
    return BacktestResult(
        capital, capital * (1.12 - penalty), .12 - penalty, .08, -.12 - penalty,
        .15, 0.9 - penalty, [fill],
        [{"date": "2025-01-01", "equity": capital, "pending_orders": 0},
         {"date": "2025-01-02", "equity": capital * (1.12 - penalty), "pending_orders": 0}],
        {"lot_size": 100, "max_daily_amount_participation": .01,
         "max_daily_volume_participation": .01, "pending_orders_at_end": 0},
        [{"signal_day": "2025-01-01", "day": "2025-01-01", "symbol": "000001.SZ",
          "side": "buy", "stage": "initial", "status": "created", "remaining_shares": 1000},
         {"signal_day": "2025-01-01", "day": "2025-01-02", "symbol": "000001.SZ",
          "side": "buy", "stage": "initial", "status": "filled", "remaining_shares": 0}],
    )


@pytest.fixture
def snapshot():
    bar = Bar("000001.SZ", "测试", date(2025, 1, 2), 10, 10, 10, 10,
              100_000, 1_000_000, "主题", "行业")
    return DataSnapshot(datetime(2026, 7, 6, tzinfo=timezone.utc), [bar], "deterministic-demo", 1)


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


def test_parameter_selection_snapshot_removes_final_dates_and_source_metadata():
    from quant_system.models import Bar

    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(100)]
    bars = [Bar("000001.SZ", "测试", day, 10, 10, 10, 10, 1000, 10_000, "主题", "行业") for day in days]
    original = DataSnapshot(
        datetime(2020, 4, 9, 18, tzinfo=timezone.utc), bars, "fixture", 1,
        {"market_inputs": {"global_risk_score": 99}, "research_baselines": {"future": "secret"}},
    )
    split = make_time_series_split(days, embargo_observations=2)
    restricted, scope = make_parameter_selection_snapshot(original, split)
    assert max(bar.day.isoformat() for bar in restricted.bars) < split.windows[-1].start
    assert restricted.as_of.date().isoformat() < split.windows[-1].start
    assert scope.overlaps_final_test is False
    assert scope.source_metadata_forwarded == ()
    assert set(restricted.metadata) == {"research_scope"}


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


def test_baseline_contract_fails_closed_when_missing_and_evaluates_explicit_series():
    days = ("2025-01-02", "2025-01-03")
    missing = evaluate_baseline_contract({}, days)
    assert missing.status == "missing" and not missing.results
    assert missing.missing_series == ("沪深300", "中证全指", "简单动量")

    contract = {
        "research_baselines": {
            "schema_version": "research-baselines/v1",
            "series": {
                name: {
                    "returns_by_date": {days[0]: .01, days[1]: -.005},
                    "source": f"licensed-fixture:{name}",
                    "is_official_point_in_time": name != "简单动量",
                }
                for name in ("沪深300", "中证全指", "简单动量")
            },
        }
    }
    unverified = evaluate_baseline_contract(contract, days)
    assert unverified.status == "unverified"
    assert len(unverified.results) == 3
    assert any("简单动量" in error for error in unverified.errors)
    assert all(item.observations == 2 and "licensed-fixture" in item.source for item in unverified.results)

    contract["research_baselines"]["series"]["简单动量"]["is_official_point_in_time"] = True
    available = evaluate_baseline_contract(contract, days)
    assert available.status == "available"

    del contract["research_baselines"]["series"]["沪深300"]["returns_by_date"][days[1]]
    incomplete = evaluate_baseline_contract(contract, days)
    assert incomplete.status == "missing" and incomplete.missing_dates["沪深300"] == (days[1],)


def test_four_exact_capital_tiers_and_capacity_constraints(snapshot):
    results = evaluate_capacity(snapshot, runner=lambda snap, **kwargs: fake_result(**kwargs))
    assert tuple(x.capital for x in results) == CAPITAL_TIERS
    assert all(x.executable and x.lot_size == 100 and x.max_participation == .01 for x in results)
    with pytest.raises(ValueError, match="exactly"):
        evaluate_capacity(snapshot, (100_000,), runner=lambda snap, **kwargs: fake_result(**kwargs))


def test_capacity_fails_with_zero_fills_and_999_pending_orders(snapshot):
    def impossible(_snapshot, *, capital, **_kwargs):
        return BacktestResult(
            capital, capital, 0, 0, 0, 0, 0, [],
            [{"date": "2025-01-02", "equity": capital, "pending_orders": 999}],
            {"lot_size": 100, "max_daily_volume_participation": .01,
             "pending_orders_at_end": 999},
            [{"signal_day": "2025-01-01", "day": "2025-01-02", "symbol": "000001.SZ",
              "side": "buy", "stage": "initial", "status": "blocked",
              "remaining_shares": 999}],
        )

    capacities = evaluate_capacity(snapshot, runner=impossible)
    assert len(capacities) == 4
    assert all(not item.executable and item.fills == 0 for item in capacities)
    assert all(item.pending_orders_at_end == 999 and item.unresolved_shares == 999 for item in capacities)
    result = evaluate_gates(credible_metrics(capacity_executable=tuple(item.executable for item in capacities)))
    gate = next(item for item in result["gates"] if item["id"] == "BT-007-CAPACITY")
    assert gate["status"] == "FAIL"


def test_parameter_sensitivity_marks_neighbor_stability(snapshot):
    results = parameter_sensitivity(snapshot, runner=lambda snap, **kwargs: fake_result(**kwargs))
    assert len(results) == 9
    assert any(x.parameters == {"rebalance_days": 10, "slippage_bps": 8.0} for x in results)
    assert sum(x.stable_neighbor for x in results) >= 6


def test_walk_forward_replays_disjoint_oos_folds_with_past_only_inputs():
    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(30)]
    bars = [Bar("000001.SZ", "测试", day, 10, 10, 10, 10, 100_000, 1_000_000,
                "主题", "行业") for day in days]
    source = DataSnapshot(datetime(2025, 1, 30, tzinfo=timezone.utc), bars, "fixture", 1)
    observed = []

    def runner(fold_snapshot, *, capital, **_kwargs):
        last = max(bar.day for bar in fold_snapshot.bars)
        observed.append((last, fold_snapshot.as_of.date()))
        curve = [{"date": day.isoformat(), "equity": capital + index, "pending_orders": 0}
                 for index, day in enumerate(sorted({bar.day for bar in fold_snapshot.bars}))]
        return BacktestResult(capital, curve[-1]["equity"], .01, .01, 0, 0, 0, [], curve,
                              {"pending_orders_at_end": 0})

    result = walk_forward_oos(source, oos_dates=days[-9:], capital=100_000, runner=runner)
    assert result.status == "complete" and len(result.folds) == 3
    assert result.independent_non_overlapping and result.aggregate_oos_observations == 9
    assert [item["date"] for item in result.aggregate_returns_by_date] == [day.isoformat() for day in days[-9:]]
    assert all(last == as_of for last, as_of in observed)
    assert all(fold.train_end < fold.oos_start <= fold.oos_end for fold in result.folds)


def test_ablation_removes_each_core_factor_once():
    calls = []
    def evaluator(active):
        calls.append(active)
        return len(active) / 10
    results = factor_ablation(evaluator, evaluator_label="shared-engine-fixture")
    assert tuple(x.removed_factor for x in results) == CORE_FACTORS
    assert all(x.delta_from_full == -.1 for x in results)
    assert len(calls) == 1 + len(CORE_FACTORS)


def test_stress_scenarios_rerun_engine_and_fail_closed_without_real_block(snapshot):
    calls = []
    base = fake_result()

    def runner(input_snapshot, **kwargs):
        calls.append((input_snapshot.metadata.get("research_stress_input"), kwargs))
        return fake_result(**kwargs)

    results = stress_scenarios(snapshot, capital=100_000, base_result=base, runner=runner)
    assert len(results) == 3 and len(calls) == 3
    assert all(item.engine_rerun and item.input_snapshot_hash for item in results)
    doubled = next(item for item in results if item.name == "交易成本翻倍")
    assert calls[0][1]["transaction_cost_multiplier"] == 2
    assert calls[0][1]["slippage_bps"] == 16
    blocked = next(item for item in results if item.name == "连续无法成交")
    assert blocked.status == "inconclusive" and blocked.blocked_events == 0


def credible_metrics(**overrides):
    values = dict(oos_max_drawdown=-.13, average_holdings=4, median_holding_days=57,
                  after_cost_excess_return=.06, max_year_contribution=.35,
                  max_theme_contribution=.40, max_stock_contribution=.20,
                  neighbor_stability_ratio=.78, baseline_count=3, capacity_tier_count=4,
                  capacity_executable=(True, True, True, True),
                  walk_forward_oos_complete=True, stress_evidence_complete=True,
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


def test_missing_contribution_evidence_fails_closed_without_placeholder_values():
    result = evaluate_gates(credible_metrics(
        max_year_contribution=None, max_theme_contribution=None, max_stock_contribution=None,
    ))
    gate = next(item for item in result["gates"] if item["id"] == "BT-008-CONCENTRATION")
    assert gate["status"] == "FAIL"
    assert gate["actual"] == {"year": None, "theme": None, "stock": None}
    assert "不可计算" in gate["note"]


def test_trade_statistics_and_contributions_are_derived_from_fills_and_equity():
    fills = [
        Fill(date(2024, 1, 2), date(2024, 1, 3), "A.SH", "buy", 10, 10, 0),
        Fill(date(2024, 6, 1), date(2024, 6, 2), "A.SH", "sell", 20, 10, 0),
        Fill(date(2025, 1, 2), date(2025, 1, 3), "B.SH", "buy", 10, 10, 0),
        Fill(date(2026, 1, 2), date(2026, 1, 3), "B.SH", "sell", 20, 10, 0),
    ]
    curve = [
        {"date": "2024-01-01", "equity": 1_000, "positions": 0},
        {"date": "2024-12-31", "equity": 1_100, "positions": 0},
        {"date": "2025-12-31", "equity": 1_100, "positions": 1},
        {"date": "2026-12-31", "equity": 1_200, "positions": 0},
    ]
    bars = [
        Bar("A.SH", "A", date(2024, 1, 1), 10, 10, 10, 10, 1_000, 10_000, "主题A", "行业A"),
        Bar("A.SH", "A", date(2026, 12, 31), 20, 20, 20, 20, 1_000, 20_000, "主题A", "行业A"),
        Bar("B.SH", "B", date(2025, 1, 1), 10, 10, 10, 10, 1_000, 10_000, "主题B", "行业B"),
        Bar("B.SH", "B", date(2026, 12, 31), 20, 20, 20, 20, 1_000, 20_000, "主题B", "行业B"),
    ]
    result = BacktestResult(1_000, 1_200, .2, .1, 0, .1, 1, fills, curve, {})
    snapshot = DataSnapshot(datetime(2026, 12, 31, tzinfo=timezone.utc), bars, "fixture", 2)

    statistics = evaluate_trade_statistics(fills, curve, evaluation_start="2024-01-01")
    attribution = evaluate_contribution_attribution(result, snapshot, evaluation_start="2024-01-01")

    assert statistics.completed_trades == 2 and statistics.win_rate == 1
    assert statistics.traded_notional == 600
    assert attribution.status == "available"
    assert attribution.stock.contributions == {"A.SH": 100.0, "B.SH": 100.0}
    assert attribution.theme.contributions == {"主题A": 100.0, "主题B": 100.0}
    assert attribution.year.max_absolute_share == .5
    assert attribution.stock.max_absolute_share == .5


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
