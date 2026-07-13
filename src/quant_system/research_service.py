"""Build immutable, explicitly non-production research evidence packages."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from statistics import mean, median
from typing import Any
from uuid import uuid4

from .backtest import run_backtest
from .engine import MODEL_VERSION
from .models import DataSnapshot
from .research import (
    GateInput, canonical_json, evaluate_baselines, evaluate_capacity,
    evaluate_gates, factor_ablation, make_manifest, make_time_series_split,
    parameter_sensitivity, stress_scenarios, write_evidence_package,
)


def _daily_market_returns(snapshot: DataSnapshot) -> list[float]:
    by_symbol: dict[str, list] = {}
    for bar in snapshot.bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    by_day: dict[str, list[float]] = {}
    for bars in by_symbol.values():
        bars.sort(key=lambda x: x.day)
        for previous, current in zip(bars, bars[1:]):
            if previous.close:
                by_day.setdefault(current.day.isoformat(), []).append(current.close / previous.close - 1)
    return [mean(by_day[day]) for day in sorted(by_day) if by_day[day]]


def _strategy_returns(curve: list[dict]) -> list[float]:
    values = [float(x["equity"]) for x in curve]
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]


def _curve_summary(curve: list[dict]) -> dict[str,float]:
    values=[float(x["equity"]) for x in curve]
    if not values:return {"total_return":0.0,"max_drawdown":0.0}
    peak=values[0];mdd=0.0
    for value in values:
        peak=max(peak,value);mdd=min(mdd,value/peak-1 if peak else 0)
    return {"total_return":round(values[-1]/values[0]-1,6) if values[0] else 0.0,"max_drawdown":round(mdd,6)}


def _median_holding_days(fills: list) -> float:
    opened: dict[str, Any] = {}; durations: list[int] = []
    for fill in fills:
        if fill.side == "buy": opened[fill.symbol] = fill.fill_day
        elif fill.symbol in opened:
            durations.append((fill.fill_day - opened.pop(fill.symbol)).days)
    return float(median(durations)) if durations else 0.0


def run_research_package(snapshot: DataSnapshot, output_root: str | Path = "data/research",
                         capital: int = 1_000_000, simulation_weeks: float = 0) -> dict[str, Any]:
    run_id = str(uuid4())
    dates = sorted({bar.day for bar in snapshot.bars})
    split = make_time_series_split(dates, embargo_observations=2)
    strategy = run_backtest(snapshot, capital=capital)
    final_test_start=split.windows[-1].start
    oos_curve=[x for x in strategy.equity_curve if x["date"]>=final_test_start]
    oos_summary=_curve_summary(oos_curve)
    strategy_returns = _strategy_returns(oos_curve)
    broad = _daily_market_returns(snapshot)
    if not broad:
        raise ValueError("research requires at least two market observations")
    # These are transparent demo proxies, never presented as licensed official index series.
    oos_length=max(1,len(strategy_returns));broad_oos=broad[-oos_length:]
    baseline_series = {
        "沪深300": [x * .90 for x in broad_oos],
        "中证全指": broad_oos,
        "简单动量": [x * 1.05 for x in broad_oos],
    }
    sources = {name: f"{snapshot.provider}:cross-sectional-proxy" for name in baseline_series}
    baselines = evaluate_baselines(baseline_series, sources, {name: False for name in baseline_series})
    capacities = evaluate_capacity(snapshot)
    sensitivity = parameter_sensitivity(snapshot, capital=capital)
    stresses = stress_scenarios(strategy_returns or [0.0])
    ablations = factor_ablation(
        lambda active: run_backtest(snapshot,capital=capital,active_factors=active).total_return,
        evaluator_label="shared-engine-active-factor-mask",
    )
    average_holdings = mean(float(x["positions"]) for x in oos_curve) if oos_curve else 0.0
    stability = mean(1.0 if x.stable_neighbor else 0.0 for x in sensitivity)
    broad_total = next(x.total_return for x in baselines if x.name == "中证全指")
    gates = evaluate_gates(GateInput(
        oos_max_drawdown=oos_summary["max_drawdown"],
        average_holdings=average_holdings,
        median_holding_days=_median_holding_days(strategy.fills),
        after_cost_excess_return=oos_summary["total_return"] - broad_total,
        # Contribution attribution needs licensed PIT constituents; fail closed until available.
        max_year_contribution=1.0, max_theme_contribution=1.0, max_stock_contribution=1.0,
        neighbor_stability_ratio=stability, baseline_count=len(baselines),
        capacity_tier_count=len(capacities), final_test_isolated=split.final_test_isolated,
        point_in_time_verified=bool(snapshot.metadata.get("point_in_time_verified", False)),
        production_data_authorized=bool(snapshot.metadata.get("production_data_authorized", False)),
        simulation_observation_weeks=simulation_weeks,
    ))
    manifest = make_manifest(
        git_commit="working-tree", model_version=MODEL_VERSION, feature_version="features-0.1.0",
        data_version=f"{snapshot.provider}:{snapshot.as_of.isoformat()}",
        config={"capital": capital, "execution": strategy.assumptions}, random_seed=0,
        split=split, provider=snapshot.provider,
    )
    report = {
        "schema_version": "research-report/v1", "run_id": run_id,
        "generated_at": datetime.now().astimezone().isoformat(),
        "provider": snapshot.provider, "candidate_label": gates["candidate_label"],
        "strategy": {k: v for k, v in asdict(strategy).items() if k not in {"fills", "equity_curve"}},
        "final_test": {"start":final_test_start,"observations":len(oos_curve),**oos_summary,
                       "isolated_from_parameter_selection":True},
        "baselines": [asdict(x) for x in baselines],
        "capacity": [asdict(x) for x in capacities],
        "sensitivity": [asdict(x) for x in sensitivity],
        "stress": [asdict(x) for x in stresses],
        "ablation": {"status": "completed", "items": [asdict(x) for x in ablations],
                     "evaluator": "共享生产策略引擎 active-factor mask"},
        "limitations": [
            "当前指数序列是确定性演示代理，不是授权的官方历史成分数据",
            "PIT、数据授权、贡献归因及连续8至12周模拟观察未完成，因此门禁必须失败",
        ],
    }
    target = Path(output_root) / run_id
    hashes = write_evidence_package(target, manifest, gates, report)
    summary = {"id": run_id, "status": "completed", "overall": gates["overall"],
               "candidate_label": gates["candidate_label"], "provider": snapshot.provider,
               "created_at": report["generated_at"], "artifacts": sorted(hashes)}
    (target / "summary.json").write_text(canonical_json(summary) + "\n", encoding="utf-8")
    return {**summary, "gates": gates["gates"], "report": report}


def read_research_run(run_id: str, output_root: str | Path = "data/research") -> dict[str, Any] | None:
    path = Path(output_root) / run_id / "summary.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else None


def read_research_artifact(run_id: str, artifact: str, output_root: str | Path = "data/research") -> dict[str, Any] | None:
    allowed = {"manifest.json", "gates.json", "research_report.json", "hashes.json", "summary.json"}
    if artifact not in allowed:
        return None
    path = Path(output_root) / run_id / artifact
    return json.loads(path.read_text("utf-8")) if path.is_file() else None
