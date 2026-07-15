"""Research validation and immutable evidence helpers.

This module deliberately separates *executing a validation protocol* from
*claiming that the protocol proves production fitness*.  Deterministic demo
data can exercise every code path, but cannot prove point-in-time correctness,
licensed benchmark membership, investment performance, or the required
8--12 week live simulation observation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Sequence

from .backtest import BacktestResult, run_backtest
from .models import DataSnapshot


CAPITAL_TIERS = (100_000, 1_000_000, 3_000_000, 10_000_000)
REQUIRED_BASELINES = ("沪深300", "中证全指", "简单动量")
CORE_FACTORS = ("market_regime", "theme_score", "stock_score", "risk_control")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TimeWindow:
    name: str
    start: str
    end: str
    observations: int
    may_tune: bool


@dataclass(frozen=True)
class TimeSeriesSplit:
    method: str
    windows: tuple[TimeWindow, ...]
    embargo_observations: int
    final_test_isolated: bool
    declaration: str
    dates_hash: str


@dataclass(frozen=True)
class ResearchInputScope:
    purpose: str
    start: str
    end: str
    observations: int
    dates_hash: str
    final_test_start: str
    overlaps_final_test: bool
    source_metadata_forwarded: tuple[str, ...]


def make_time_series_split(
    dates: Iterable[date | str], train_ratio: float = .6, validation_ratio: float = .2,
    embargo_observations: int = 0,
) -> TimeSeriesSplit:
    """Create a chronological, non-overlapping train/validation/final-test split."""
    values = sorted({d.isoformat() if isinstance(d, date) else str(d) for d in dates})
    if len(values) < 15:
        raise ValueError("time-series split requires at least 15 distinct observations")
    if not 0 < train_ratio < 1 or not 0 < validation_ratio < 1 or train_ratio + validation_ratio >= 1:
        raise ValueError("ratios must be positive and leave a final test segment")
    if embargo_observations < 0:
        raise ValueError("embargo_observations cannot be negative")
    n = len(values); train_end = max(1, int(n * train_ratio)); val_end = max(train_end + 1, int(n * (train_ratio + validation_ratio)))
    train = values[:train_end]
    validation = values[min(n, train_end + embargo_observations):val_end]
    test = values[min(n, val_end + embargo_observations):]
    if not validation or not test:
        raise ValueError("embargo leaves an empty validation or final-test segment")
    windows = (
        TimeWindow("train", train[0], train[-1], len(train), True),
        TimeWindow("validation", validation[0], validation[-1], len(validation), True),
        TimeWindow("final_test", test[0], test[-1], len(test), False),
    )
    return TimeSeriesSplit(
        method="chronological_holdout",
        windows=windows,
        embargo_observations=embargo_observations,
        final_test_isolated=True,
        declaration="最终测试集禁止参与参数选择；仅冻结模型后执行一次。",
        dates_hash=content_hash(values),
    )


def make_parameter_selection_snapshot(
    snapshot: DataSnapshot, split: TimeSeriesSplit,
) -> tuple[DataSnapshot, ResearchInputScope]:
    """Build an input that cannot expose final-test bars or full-snapshot metadata."""
    validation = next(window for window in split.windows if window.name == "validation")
    final_test = next(window for window in split.windows if window.name == "final_test")
    validation_end = date.fromisoformat(validation.end)
    bars = [bar for bar in snapshot.bars if bar.day <= validation_end]
    dates = sorted({bar.day.isoformat() for bar in bars})
    if not dates:
        raise ValueError("parameter-selection snapshot has no observations")
    if any(day >= final_test.start for day in dates):
        raise ValueError("parameter-selection snapshot overlaps final test")
    scope = ResearchInputScope(
        purpose="parameter_selection_sensitivity_ablation",
        start=dates[0], end=dates[-1], observations=len(dates),
        dates_hash=content_hash(dates), final_test_start=final_test.start,
        overlaps_final_test=False, source_metadata_forwarded=(),
    )
    restricted = DataSnapshot(
        datetime.combine(validation_end, snapshot.as_of.timetz()), bars,
        snapshot.provider, snapshot.expected_symbols,
        {"research_scope": _jsonable(scope)},
    )
    if restricted.as_of.date().isoformat() >= final_test.start:
        raise ValueError("parameter-selection as_of overlaps final test")
    return restricted, scope


@dataclass(frozen=True)
class BaselineResult:
    name: str
    total_return: float
    max_drawdown: float
    observations: int
    source: str
    is_official_point_in_time: bool


@dataclass(frozen=True)
class BaselineContractEvaluation:
    status: str
    schema_version: str | None
    required_dates: tuple[str, ...]
    missing_series: tuple[str, ...]
    missing_dates: dict[str, tuple[str, ...]]
    errors: tuple[str, ...]
    results: tuple[BaselineResult, ...]


def _curve_metrics(returns: Sequence[float]) -> tuple[float, float]:
    equity = 1.0; peak = 1.0; drawdown = 0.0
    for item in returns:
        equity *= 1 + float(item); peak = max(peak, equity); drawdown = min(drawdown, equity / peak - 1)
    return round(equity - 1, 6), round(drawdown, 6)


def evaluate_baselines(
    series: Mapping[str, Sequence[float]], sources: Mapping[str, str] | None = None,
    official_point_in_time: Mapping[str, bool] | None = None,
) -> tuple[BaselineResult, ...]:
    """Evaluate the three mandatory baseline return series without inventing data."""
    missing = [name for name in REQUIRED_BASELINES if name not in series]
    if missing:
        raise ValueError(f"missing required baseline series: {', '.join(missing)}")
    sources = sources or {}; official_point_in_time = official_point_in_time or {}
    results = []
    for name in REQUIRED_BASELINES:
        values = tuple(float(x) for x in series[name])
        if not values:
            raise ValueError(f"baseline {name} has no observations")
        total, mdd = _curve_metrics(values)
        results.append(BaselineResult(name, total, mdd, len(values), sources.get(name, "unspecified"), bool(official_point_in_time.get(name, False))))
    return tuple(results)


def evaluate_baseline_contract(
    metadata: Mapping[str, Any], required_dates: Sequence[str],
) -> BaselineContractEvaluation:
    """Evaluate only explicit, source-labelled, date-aligned baseline series.

    ``metadata.research_baselines`` must use ``research-baselines/v1`` and
    provide ``returns_by_date`` for every final-test return date.  There is no
    market-return or strategy-return fallback.
    """
    required = tuple(dict.fromkeys(str(day) for day in required_dates))
    contract = metadata.get("research_baselines") if isinstance(metadata, Mapping) else None
    if not isinstance(contract, Mapping):
        return BaselineContractEvaluation(
            "missing", None, required, REQUIRED_BASELINES, {},
            ("snapshot.metadata.research_baselines is missing",), (),
        )
    schema = contract.get("schema_version")
    if schema != "research-baselines/v1":
        return BaselineContractEvaluation(
            "invalid", str(schema) if schema is not None else None, required,
            REQUIRED_BASELINES, {},
            ("baseline contract schema_version must be research-baselines/v1",), (),
        )
    series = contract.get("series")
    if not isinstance(series, Mapping):
        return BaselineContractEvaluation(
            "invalid", str(schema), required, REQUIRED_BASELINES, {},
            ("baseline contract series must be a mapping",), (),
        )
    missing_series = tuple(name for name in REQUIRED_BASELINES if name not in series)
    missing_dates: dict[str, tuple[str, ...]] = {}
    errors: list[str] = []
    prepared: dict[str, list[float]] = {}
    sources: dict[str, str] = {}
    official: dict[str, bool] = {}
    for name in REQUIRED_BASELINES:
        item = series.get(name)
        if item is None:
            continue
        if not isinstance(item, Mapping):
            errors.append(f"baseline {name} must be a mapping")
            continue
        source = item.get("source")
        returns_by_date = item.get("returns_by_date")
        if not isinstance(source, str) or not source.strip():
            errors.append(f"baseline {name} source is missing")
        if not isinstance(returns_by_date, Mapping):
            errors.append(f"baseline {name} returns_by_date must be a mapping")
            continue
        absent = tuple(day for day in required if day not in returns_by_date)
        if absent:
            missing_dates[name] = absent
            continue
        values: list[float] = []
        for day in required:
            try:
                value = float(returns_by_date[day])
            except (TypeError, ValueError):
                errors.append(f"baseline {name} has a non-numeric return on {day}")
                break
            if not isfinite(value) or value <= -1:
                errors.append(f"baseline {name} has an invalid return on {day}")
                break
            values.append(value)
        else:
            prepared[name] = values
            sources[name] = source.strip() if isinstance(source, str) else ""
            official[name] = bool(item.get("is_official_point_in_time", False))
    if missing_series or missing_dates:
        return BaselineContractEvaluation(
            "missing", str(schema), required, missing_series, missing_dates,
            tuple(errors), (),
        )
    if errors or len(prepared) != len(REQUIRED_BASELINES):
        return BaselineContractEvaluation(
            "invalid", str(schema), required, missing_series, missing_dates,
            tuple(errors or ["baseline contract could not be evaluated"]), (),
        )
    return BaselineContractEvaluation(
        "available", str(schema), required, (), {}, (),
        evaluate_baselines(prepared, sources, official),
    )


@dataclass(frozen=True)
class CapacityResult:
    capital: int
    final_equity: float
    total_return: float
    max_drawdown: float
    fills: int
    max_participation: float
    lot_size: int
    executable: bool


def evaluate_capacity(snapshot: DataSnapshot, capitals: Sequence[int] = CAPITAL_TIERS,
                      runner: Callable[..., BacktestResult] = run_backtest) -> tuple[CapacityResult, ...]:
    if tuple(capitals) != CAPITAL_TIERS:
        raise ValueError(f"capacity evidence must use exactly {CAPITAL_TIERS}")
    output = []
    for capital in capitals:
        result = runner(snapshot, capital=capital)
        max_participation = float(result.assumptions.get("max_daily_amount_participation", 0))
        lot_size = int(result.assumptions.get("lot_size", 0))
        output.append(CapacityResult(capital, result.final_equity, result.total_return, result.max_drawdown,
                                     len(result.fills), max_participation, lot_size,
                                     max_participation <= .02 and lot_size == 100))
    return tuple(output)


@dataclass(frozen=True)
class SensitivityResult:
    parameters: dict[str, float | int]
    total_return: float
    max_drawdown: float
    sharpe: float
    stable_neighbor: bool


def parameter_sensitivity(snapshot: DataSnapshot, rebalance_days: Sequence[int] = (8, 10, 12),
                          slippage_bps: Sequence[float] = (5, 8, 12), capital: int = 1_000_000,
                          runner: Callable[..., BacktestResult] = run_backtest) -> tuple[SensitivityResult, ...]:
    raw = []
    for rebalance in rebalance_days:
        for slippage in slippage_bps:
            result = runner(snapshot, capital=capital, rebalance_days=rebalance, slippage_bps=slippage)
            raw.append((rebalance, float(slippage), result))
    center_return = next((r.total_return for d, s, r in raw if d == 10 and s == 8), median(r.total_return for _, _, r in raw))
    tolerance = max(.02, abs(center_return) * .35)
    return tuple(SensitivityResult({"rebalance_days": d, "slippage_bps": s}, r.total_return, r.max_drawdown, r.sharpe,
                                   abs(r.total_return - center_return) <= tolerance) for d, s, r in raw)


@dataclass(frozen=True)
class AblationResult:
    removed_factor: str
    metric: float
    delta_from_full: float
    evaluator_label: str


def factor_ablation(evaluator: Callable[[frozenset[str]], float], factors: Sequence[str] = CORE_FACTORS,
                    evaluator_label: str = "caller-supplied") -> tuple[AblationResult, ...]:
    """Run leave-one-factor-out evaluation using the caller's production-shared evaluator."""
    factor_set = frozenset(factors)
    if len(factor_set) < 2:
        raise ValueError("ablation requires at least two factors")
    full = float(evaluator(factor_set))
    output = []
    for factor in factors:
        metric = float(evaluator(factor_set - {factor}))
        output.append(AblationResult(factor, metric, round(metric - full, 8), evaluator_label))
    return tuple(output)


@dataclass(frozen=True)
class StressResult:
    name: str
    total_return: float
    max_drawdown: float
    assumption: str


def stress_scenarios(returns: Sequence[float]) -> tuple[StressResult, ...]:
    if not returns:
        raise ValueError("stress scenarios require returns")
    base = [float(x) for x in returns]
    scenarios = {
        "费用与滑点翻倍": ([x - .0008 for x in base], "每期额外扣减8bp，作为成本恶化代理"),
        "单日跳空冲击": (base[:len(base)//2] + [base[len(base)//2] - .10] + base[len(base)//2+1:], "中点交易日额外-10%冲击"),
        "连续无法成交": ([0.0 if i in range(len(base)//3, len(base)//3 + 3) else x for i, x in enumerate(base)], "连续3期收益冻结，代理停牌/涨跌停阻塞"),
    }
    return tuple(StressResult(name, *_curve_metrics(values), assumption) for name, (values, assumption) in scenarios.items())


@dataclass(frozen=True)
class GateInput:
    oos_max_drawdown: float
    average_holdings: float
    median_holding_days: float
    after_cost_excess_return: float | None
    max_year_contribution: float
    max_theme_contribution: float
    max_stock_contribution: float
    neighbor_stability_ratio: float
    baseline_count: int
    capacity_tier_count: int
    final_test_isolated: bool
    point_in_time_verified: bool
    production_data_authorized: bool
    simulation_observation_weeks: float


@dataclass(frozen=True)
class GateDecision:
    id: str
    status: str
    actual: Any
    requirement: str
    note: str


def evaluate_gates(metrics: GateInput) -> dict[str, Any]:
    def gate(identifier: str, passed: bool, actual: Any, requirement: str, note: str = "") -> GateDecision:
        return GateDecision(identifier, "PASS" if passed else "FAIL", actual, requirement, note)
    gates = (
        gate("BT-008-DRAWDOWN-TARGET", metrics.oos_max_drawdown >= -.15, metrics.oos_max_drawdown, "样本外最大回撤目标不超过15%", "优化目标；不是风险保证"),
        gate("BT-008-DRAWDOWN-HARD", metrics.oos_max_drawdown >= -.18, metrics.oos_max_drawdown, "样本外最大回撤不得超过18%"),
        gate("BT-008-HOLDINGS", 3 <= metrics.average_holdings <= 5, metrics.average_holdings, "平均持仓3至5只"),
        gate("BT-008-HOLDING-DAYS", 40 <= metrics.median_holding_days <= 80, metrics.median_holding_days, "持仓中位数40至80个交易日"),
        gate("BT-008-EXCESS", metrics.after_cost_excess_return is not None and metrics.after_cost_excess_return > 0,
             metrics.after_cost_excess_return, "扣费后样本外超额收益为正",
             "缺少显式中证全指基线，无法计算超额收益" if metrics.after_cost_excess_return is None else ""),
        gate("BT-008-CONCENTRATION", max(metrics.max_year_contribution, metrics.max_theme_contribution, metrics.max_stock_contribution) <= .50,
             {"year": metrics.max_year_contribution, "theme": metrics.max_theme_contribution, "stock": metrics.max_stock_contribution}, "单一年份、题材或股票贡献均不超过50%"),
        gate("BT-008-STABILITY", metrics.neighbor_stability_ratio >= .67, metrics.neighbor_stability_ratio, "至少三分之二邻近参数保持稳定"),
        gate("BT-005-FINAL-TEST", metrics.final_test_isolated, metrics.final_test_isolated, "最终测试集未参与调参"),
        gate("BT-006-BASELINES", metrics.baseline_count >= 3, metrics.baseline_count, "沪深300、中证全指、简单动量三基线",
             "快照缺少完整、逐日对齐的显式基线序列" if metrics.baseline_count < 3 else ""),
        gate("BT-007-CAPACITY", metrics.capacity_tier_count == 4, metrics.capacity_tier_count, "10万、100万、300万、1000万四档容量"),
        gate("DAT-PIT", metrics.point_in_time_verified, metrics.point_in_time_verified, "真实数据通过PIT与历史成分验证", "Demo不能证明此项"),
        gate("DATA-LICENSE", metrics.production_data_authorized, metrics.production_data_authorized, "生产数据授权已书面确认"),
        gate("SIM-003-OBSERVATION", metrics.simulation_observation_weeks >= 8, metrics.simulation_observation_weeks, "真实连续模拟观察至少8周", "自动化测试不能替代时间"),
    )
    serialized = [_jsonable(g) for g in gates]
    return {"overall": "PASS" if all(g.status == "PASS" for g in gates) else "FAIL", "gates": serialized,
            "candidate_label": "可进入发布评审" if all(g.status == "PASS" for g in gates) else "工程候选版/模拟观察中"}


def make_manifest(*, git_commit: str, model_version: str, feature_version: str, data_version: str,
                  config: Mapping[str, Any], random_seed: int, split: TimeSeriesSplit,
                  provider: str, generated_at: datetime | None = None) -> dict[str, Any]:
    body = {
        "schema_version": "research-manifest/v1", "git_commit": git_commit,
        "model_version": model_version, "feature_version": feature_version, "data_version": data_version,
        "config_hash": content_hash(config), "random_seed": random_seed, "split": _jsonable(split),
        "provider": provider, "generated_at": (generated_at or datetime.now().astimezone()).isoformat(),
        "limitations": ["Demo数据不能证明真实PIT、历史成分或策略收益", "代码与自动化测试不能替代真实连续8至12周模拟观察"],
    }
    return {**body, "manifest_hash": content_hash(body)}


def write_evidence_package(output_dir: str | Path, manifest: Mapping[str, Any], gates: Mapping[str, Any],
                           report: Mapping[str, Any]) -> dict[str, str]:
    """Write canonical artifacts; refuse to mutate an existing evidence directory."""
    target = Path(output_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"evidence directory is immutable and already populated: {target}")
    target.mkdir(parents=True, exist_ok=True)
    artifacts = {"manifest.json": manifest, "gates.json": gates, "research_report.json": report}
    hashes: dict[str, str] = {}
    for name, payload in artifacts.items():
        text = canonical_json(payload) + "\n"
        (target / name).write_text(text, encoding="utf-8")
        hashes[name] = sha256(text.encode("utf-8")).hexdigest()
    index = {"schema_version": "research-evidence-index/v1", "artifacts": hashes}
    index_text = canonical_json(index) + "\n"
    (target / "hashes.json").write_text(index_text, encoding="utf-8")
    hashes["hashes.json"] = sha256(index_text.encode("utf-8")).hexdigest()
    return hashes
