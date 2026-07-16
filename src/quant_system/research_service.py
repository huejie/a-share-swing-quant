"""Build immutable, explicitly non-production research evidence packages."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
import platform
from pathlib import Path
from statistics import mean
import sys
from typing import Any
from uuid import uuid4

from .backtest import run_backtest
from .engine import MODEL_VERSION
from .models import DataSnapshot
from .research import (
    GateInput, canonical_json, content_hash, evaluate_baseline_contract, evaluate_capacity,
    evaluate_contribution_attribution, evaluate_gates, evaluate_trade_statistics,
    factor_ablation, make_manifest, make_time_series_split,
    make_parameter_selection_snapshot, parameter_sensitivity, stress_scenarios, walk_forward_oos,
    write_evidence_package,
)


def _curve_summary(curve: list[dict]) -> dict[str,float]:
    values=[float(x["equity"]) for x in curve]
    if not values:return {"total_return":0.0,"max_drawdown":0.0}
    peak=values[0];mdd=0.0
    for value in values:
        peak=max(peak,value);mdd=min(mdd,value/peak-1 if peak else 0)
    return {"total_return":round(values[-1]/values[0]-1,6) if values[0] else 0.0,"max_drawdown":round(mdd,6)}


def _explicit_research_claim(metadata: dict[str, Any], canonical: str, aliases: tuple[str, ...]) -> bool:
    """Read only explicit true claims; a canonical false cannot be bypassed by an alias."""
    if canonical in metadata:
        return metadata.get(canonical) is True
    for alias in aliases:
        if metadata.get(alias) is True:
            if alias == "authorized":
                return bool(str(metadata.get("authorization_scope") or "").strip())
            return True
    if canonical == "production_data_authorized":
        authorization = metadata.get("authorization")
        return bool(isinstance(authorization, dict) and authorization.get("authorized") is True
                    and str(authorization.get("scope") or "").strip())
    if canonical == "point_in_time_verified":
        pit = metadata.get("pit")
        return bool(isinstance(pit, dict) and pit.get("verified") is True
                    and str(pit.get("method") or "").strip())
    return False


def run_research_package(snapshot: DataSnapshot, output_root: str | Path = "data/research",
                         capital: int = 1_000_000, simulation_weeks: float = 0) -> dict[str, Any]:
    run_id = str(uuid4())
    dates = sorted({bar.day for bar in snapshot.bars})
    split = make_time_series_split(dates, embargo_observations=2)
    selection_snapshot, selection_scope = make_parameter_selection_snapshot(snapshot, split)

    # Parameter diagnostics run before the frozen final-test evaluation and
    # receive only the restricted training/validation snapshot.
    frozen_parameters = {"rebalance_days": 10, "slippage_bps": 8.0}
    sensitivity = parameter_sensitivity(selection_snapshot, capital=capital, runner=run_backtest)
    ablations = factor_ablation(
        lambda active: run_backtest(selection_snapshot, capital=capital, active_factors=active).total_return,
        evaluator_label="shared-engine-active-factor-mask:tuning-only",
    )

    strategy = run_backtest(snapshot, capital=capital, **frozen_parameters)
    final_test_start=split.windows[-1].start
    final_test_dates = [day for day in dates if day.isoformat() >= final_test_start]
    walk_forward = walk_forward_oos(
        snapshot, oos_dates=final_test_dates, capital=capital,
        **frozen_parameters, fold_count=3, runner=run_backtest,
    )
    oos_curve=[x for x in strategy.equity_curve if x["date"]>=final_test_start]
    oos_summary=_curve_summary(oos_curve)
    gate_oos_summary = ({
        "total_return": walk_forward.aggregate_total_return,
        "max_drawdown": walk_forward.aggregate_max_drawdown,
    } if walk_forward.status == "complete" else oos_summary)
    baseline_dates = (tuple(item["date"] for item in walk_forward.aggregate_returns_by_date)
                      or tuple(x["date"] for x in oos_curve[1:]))
    baseline_evaluation = evaluate_baseline_contract(snapshot.metadata, baseline_dates)
    baselines = baseline_evaluation.results
    capacities = evaluate_capacity(snapshot, runner=run_backtest)
    stresses = stress_scenarios(
        snapshot, capital=capital, base_result=strategy, **frozen_parameters, runner=run_backtest,
    )
    average_holdings = mean(float(x["positions"]) for x in oos_curve) if oos_curve else 0.0
    stability = mean(1.0 if x.stable_neighbor else 0.0 for x in sensitivity)
    broad_total = next((x.total_return for x in baselines
                        if x.name == "中证全指" and x.is_official_point_in_time), None)
    official_baseline_count = sum(item.is_official_point_in_time for item in baselines)
    capacity_executable = tuple(item.executable for item in capacities)
    stress_complete = len(stresses) == 3 and all(
        item.status == "complete" and item.engine_rerun for item in stresses
    )
    trade_statistics = evaluate_trade_statistics(
        strategy.fills, strategy.equity_curve, evaluation_start=final_test_start,
    )
    attribution = evaluate_contribution_attribution(
        strategy, snapshot, evaluation_start=final_test_start,
    )
    point_in_time_verified = _explicit_research_claim(
        snapshot.metadata, "point_in_time_verified", ("pit_verified",),
    )
    production_data_authorized = _explicit_research_claim(
        snapshot.metadata, "production_data_authorized", ("authorized",),
    )
    gates = evaluate_gates(GateInput(
        oos_max_drawdown=gate_oos_summary["max_drawdown"],
        average_holdings=average_holdings,
        median_holding_days=trade_statistics.median_holding_trading_days or 0.0,
        after_cost_excess_return=(gate_oos_summary["total_return"] - broad_total
                                  if broad_total is not None else None),
        max_year_contribution=attribution.year.max_absolute_share,
        max_theme_contribution=attribution.theme.max_absolute_share,
        max_stock_contribution=attribution.stock.max_absolute_share,
        neighbor_stability_ratio=stability, baseline_count=official_baseline_count,
        capacity_tier_count=len(capacities),
        capacity_executable=capacity_executable,
        walk_forward_oos_complete=walk_forward.status == "complete",
        stress_evidence_complete=stress_complete,
        final_test_isolated=split.final_test_isolated and not selection_scope.overlaps_final_test,
        point_in_time_verified=point_in_time_verified,
        production_data_authorized=production_data_authorized,
        simulation_observation_weeks=simulation_weeks,
    ))
    snapshot_hash = content_hash({
        "as_of": snapshot.as_of, "provider": snapshot.provider,
        "expected_symbols": snapshot.expected_symbols,
        "bars": [asdict(bar) for bar in snapshot.bars], "metadata": snapshot.metadata,
    })
    lock_path = Path(__file__).resolve().parents[2] / "uv.lock"
    runtime = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "executable": Path(sys.executable).name,
        "uv_lock_sha256": (
            hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.is_file() else "missing"
        ),
    }
    run_parameters = {
        "capital": capital, "frozen_parameters": frozen_parameters, "random_seed": 0,
        "provider": snapshot.provider, "snapshot_as_of": snapshot.as_of.isoformat(),
        "snapshot_content_hash": snapshot_hash, "final_test_start": final_test_start,
        "simulation_observation_weeks": simulation_weeks,
        "runtime": runtime,
    }
    manifest = make_manifest(
        git_commit=os.getenv("QUANT_GIT_COMMIT","unknown"), model_version=MODEL_VERSION, feature_version="features-0.2.0",
        data_version=f"{snapshot.provider}:{snapshot.as_of.isoformat()}:{snapshot_hash}",
        config={**run_parameters, "execution": strategy.assumptions}, random_seed=0,
        split=split, provider=snapshot.provider,
    )
    report = {
        "schema_version": "research-report/v1", "run_id": run_id,
        "generated_at": datetime.now().astimezone().isoformat(),
        "provider": snapshot.provider, "candidate_label": gates["candidate_label"],
        "runtime": runtime,
        "strategy": {k: v for k, v in asdict(strategy).items() if k not in {"fills", "equity_curve"}},
        "performance_metrics": asdict(trade_statistics),
        "contribution_attribution": asdict(attribution),
        "data_eligibility": {
            "point_in_time_verified": point_in_time_verified,
            "production_data_authorized": production_data_authorized,
            "source_fields": {
                "point_in_time_verified": "point_in_time_verified" if "point_in_time_verified" in snapshot.metadata else "pit_verified" if "pit_verified" in snapshot.metadata else "missing",
                "production_data_authorized": "production_data_authorized" if "production_data_authorized" in snapshot.metadata else "authorized" if "authorized" in snapshot.metadata else "authorization" if "authorization" in snapshot.metadata else "missing",
            },
        },
        "final_test": {"start":final_test_start,"observations":len(oos_curve),**oos_summary,
                       "isolated_from_parameter_selection":not selection_scope.overlaps_final_test,
                       "executed_after_parameter_freeze": True,
                       "frozen_parameters": frozen_parameters},
        "walk_forward_oos": asdict(walk_forward),
        "gate_oos_metrics": {**gate_oos_summary, "source": (
            "walk_forward_oos.aggregate" if walk_forward.status == "complete" else "single_holdout_fallback"
        )},
        "parameter_experiment_scope": asdict(selection_scope),
        "baseline_contract": {k: v for k, v in asdict(baseline_evaluation).items() if k != "results"},
        "baselines": [asdict(x) for x in baselines],
        "capacity": [asdict(x) for x in capacities],
        "sensitivity": [asdict(x) for x in sensitivity],
        "stress": [asdict(x) for x in stresses],
        "ablation": {"status": "completed", "items": [asdict(x) for x in ablations],
                     "evaluator": "共享生产策略引擎 active-factor mask（仅训练/验证输入）"},
        "limitations": [
            ("快照未提供完整显式基线序列，基线与超额收益门禁失败"
             if baseline_evaluation.status != "available"
             else "三条基线均按快照显式契约、完整日期和官方PIT声明评估"),
            ("贡献归因已由最终测试权益、真实成交现金流和PIT题材计算"
             if attribution.status == "available"
             else "最终测试贡献归因不可完整计算，BT-008-CONCENTRATION门禁失败，详见contribution_attribution"),
            "PIT、数据授权及连续8至12周模拟观察须分别提供真实证据",
        ],
    }
    target = Path(output_root) / run_id
    reproduction_command = (
        "python -c \"from apps.api.main import service; from quant_system.research_service import "
        f"run_research_package; service.ensure(require_snapshot=True); print(run_research_package(service.snapshot, "
        f"r'{str(output_root)}', capital={capital}, simulation_weeks={simulation_weeks}))\""
    )
    additional_artifacts = {
        "transactions.json": {
            "schema_version": "research-transactions/v1", "run_id": run_id,
            "final_test_start": final_test_start, "count": len(strategy.fills),
            "records": [asdict(fill) for fill in strategy.fills],
        },
        "equity_curve.json": {
            "schema_version": "research-equity-curve/v1", "run_id": run_id,
            "count": len(strategy.equity_curve), "records": strategy.equity_curve,
            "final_test_records": oos_curve,
        },
        "performance_metrics.json": {
            "schema_version": "research-performance/v1", "run_id": run_id,
            **asdict(trade_statistics),
        },
        "attribution.json": {
            "schema_version": "research-attribution/v1", "run_id": run_id,
            **asdict(attribution),
        },
        "run_config.json": {
            "schema_version": "research-run-config/v1", "run_id": run_id,
            "parameters": run_parameters, "execution_assumptions": strategy.assumptions,
            "reproduction_command": reproduction_command,
            "reproduction_requirements": "使用相同commit、依赖锁、provider数据包和snapshot_content_hash；新运行会产生新run_id",
        },
        "walk_forward.json": {
            "schema_version": "research-walk-forward/v1", "run_id": run_id,
            **asdict(walk_forward),
        },
        "stress_tests.json": {
            "schema_version": "research-stress-tests/v1", "run_id": run_id,
            "complete": stress_complete, "records": [asdict(item) for item in stresses],
        },
    }
    hashes = write_evidence_package(
        target, manifest, gates, report, additional_artifacts=additional_artifacts,
    )
    summary = {"id": run_id, "status": "completed", "overall": gates["overall"],
               "candidate_label": gates["candidate_label"], "provider": snapshot.provider,
               "created_at": report["generated_at"], "artifacts": sorted(hashes)}
    (target / "summary.json").write_text(canonical_json(summary) + "\n", encoding="utf-8")
    return {**summary, "gates": gates["gates"], "report": report}


def read_research_run(run_id: str, output_root: str | Path = "data/research") -> dict[str, Any] | None:
    path = Path(output_root) / run_id / "summary.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else None


def read_research_artifact(run_id: str, artifact: str, output_root: str | Path = "data/research") -> dict[str, Any] | None:
    allowed = {"manifest.json", "gates.json", "research_report.json", "hashes.json", "summary.json",
               "transactions.json", "equity_curve.json", "performance_metrics.json",
               "attribution.json", "run_config.json", "walk_forward.json", "stress_tests.json"}
    if artifact not in allowed:
        return None
    path = Path(output_root) / run_id / artifact
    return json.loads(path.read_text("utf-8")) if path.is_file() else None
