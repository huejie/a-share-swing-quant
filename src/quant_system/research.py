"""Research validation and immutable evidence helpers.

This module deliberately separates *executing a validation protocol* from
*claiming that the protocol proves production fitness*.  Deterministic demo
data can exercise every code path, but cannot prove point-in-time correctness,
licensed benchmark membership, investment performance, or the required
8--12 week live simulation observation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Iterable, Mapping, Sequence

from .backtest import BacktestResult, run_backtest
from .models import Bar, DataSnapshot


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
    results = evaluate_baselines(prepared, sources, official)
    unofficial = tuple(item.name for item in results if not item.is_official_point_in_time)
    if unofficial:
        return BaselineContractEvaluation(
            "unverified", str(schema), required, (), {},
            tuple(f"baseline {name} is not declared official point-in-time" for name in unofficial),
            results,
        )
    return BaselineContractEvaluation("available", str(schema), required, (), {}, (), results)


@dataclass(frozen=True)
class CapacityResult:
    capital: int
    final_equity: float
    total_return: float
    max_drawdown: float
    fills: int
    max_participation: float
    configured_max_participation: float
    lot_size: int
    requested_orders: int
    blocked_events: int
    partial_events: int
    pending_orders_at_end: int
    unresolved_shares: int
    executable: bool


def evaluate_capacity(snapshot: DataSnapshot, capitals: Sequence[int] = CAPITAL_TIERS,
                      runner: Callable[..., BacktestResult] = run_backtest) -> tuple[CapacityResult, ...]:
    if tuple(capitals) != CAPITAL_TIERS:
        raise ValueError(f"capacity evidence must use exactly {CAPITAL_TIERS}")
    output = []
    volumes = {(bar.day.isoformat(), bar.symbol): max(0, int(bar.volume)) for bar in snapshot.bars}
    for capital in capitals:
        result = runner(snapshot, capital=capital)
        configured_participation = float(result.assumptions.get("max_daily_volume_participation",
                                                                result.assumptions.get("max_daily_amount_participation", 0)))
        lot_size = int(result.assumptions.get("lot_size", 0))
        filled_by_session: dict[tuple[str, str], int] = defaultdict(int)
        for fill in result.fills:
            fill_day = fill.fill_day.isoformat() if isinstance(fill.fill_day, date) else str(fill.fill_day)
            filled_by_session[(fill_day, str(fill.symbol))] += int(fill.shares)
        participation_values = [
            shares / volumes[key] for key, shares in filled_by_session.items()
            if volumes.get(key, 0) > 0
        ]
        participation_known = bool(result.fills) and len(participation_values) == len(filled_by_session)
        max_participation = max(participation_values, default=0.0)
        ledger = list(result.order_ledger or [])
        requested = sum(item.get("status") == "created" for item in ledger)
        blocked = sum(item.get("status") == "blocked" for item in ledger)
        partial = sum(item.get("status") == "partial" for item in ledger)
        latest: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        for item in ledger:
            key = (str(item.get("signal_day")), str(item.get("symbol")),
                   str(item.get("side")), str(item.get("stage")))
            latest[key] = item
        unresolved = sum(
            max(0, int(item.get("remaining_shares") or 0))
            for item in latest.values()
            if item.get("status") != "filled"
        )
        curve_pending = int(result.equity_curve[-1].get("pending_orders", 0)) if result.equity_curve else 0
        pending = max(int(result.assumptions.get("pending_orders_at_end", 0)), curve_pending)
        executable = (
            bool(result.fills) and participation_known and requested > 0
            and pending == 0 and unresolved == 0
            and max_participation <= .02 and configured_participation <= .02
            and lot_size == 100
        )
        output.append(CapacityResult(
            capital, result.final_equity, result.total_return, result.max_drawdown,
            len(result.fills), round(max_participation, 8), configured_participation, lot_size,
            requested, blocked, partial, pending, unresolved, executable,
        ))
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
    status: str
    engine_rerun: bool
    fills: int
    blocked_events: int
    pending_orders_at_end: int
    input_snapshot_hash: str


def _result_stress_metrics(result: BacktestResult) -> tuple[int, int, int]:
    blocked = sum(item.get("status") == "blocked" for item in (result.order_ledger or []))
    curve_pending = int(result.equity_curve[-1].get("pending_orders", 0)) if result.equity_curve else 0
    pending = max(int(result.assumptions.get("pending_orders_at_end", 0)), curve_pending)
    return len(result.fills), blocked, pending


def _snapshot_hash(snapshot: DataSnapshot) -> str:
    return content_hash({
        "as_of": snapshot.as_of, "provider": snapshot.provider,
        "expected_symbols": snapshot.expected_symbols,
        "bars": [asdict(bar) for bar in snapshot.bars], "metadata": snapshot.metadata,
    })


def _replace_snapshot_bars(snapshot: DataSnapshot, bars: Sequence[Bar], label: str) -> DataSnapshot:
    metadata = dict(snapshot.metadata)
    metadata["research_stress_input"] = label
    return DataSnapshot(snapshot.as_of, list(bars), snapshot.provider, snapshot.expected_symbols, metadata)


def stress_scenarios(
    snapshot: DataSnapshot, *, capital: int, base_result: BacktestResult,
    rebalance_days: int = 10, slippage_bps: float = 8.0,
    runner: Callable[..., BacktestResult] = run_backtest,
) -> tuple[StressResult, ...]:
    """Rerun the shared backtest engine on three explicit stressed inputs.

    This function intentionally does not transform an already-produced return
    array.  Every metric below comes from a new engine execution.  If the
    continuous-block scenario cannot demonstrate a real blocked order, its
    status is ``inconclusive`` and the research gate fails closed.
    """
    if not snapshot.bars:
        raise ValueError("stress scenarios require snapshot bars")
    common = {"capital": capital, "rebalance_days": rebalance_days, "slippage_bps": slippage_bps}
    cases: list[tuple[str, str, DataSnapshot, dict[str, Any], bool]] = []

    cases.append((
        "交易成本翻倍", "佣金、最低佣金、印花税及滑点均按基准的2倍通过共享回测引擎重跑",
        snapshot, {**common, "slippage_bps": slippage_bps * 2, "transaction_cost_multiplier": 2.0}, False,
    ))

    days = sorted({bar.day for bar in snapshot.bars})
    filled_days = sorted({fill.fill_day for fill in base_result.fills})
    shock_day = next((day for day in days if filled_days and day > filled_days[0]), days[len(days) // 2])
    shocked_bars = [
        replace(bar, open=bar.open * .90, high=bar.high * .90, low=bar.low * .90, close=bar.close * .90)
        if bar.day == shock_day else bar
        for bar in snapshot.bars
    ]
    impact_snapshot = _replace_snapshot_bars(snapshot, shocked_bars, f"market_gap_minus_10pct:{shock_day.isoformat()}")
    cases.append((
        "单日市场跳空冲击", f"{shock_day.isoformat()}全部可见证券OHLC下移10%，共享回测引擎重跑",
        impact_snapshot, common, False,
    ))

    created = next((item for item in (base_result.order_ledger or []) if item.get("status") == "created"), None)
    if created:
        signal_day = date.fromisoformat(str(created["signal_day"])[:10])
        symbol = str(created["symbol"])
        block_days = tuple(day for day in days if day > signal_day)[:3]
        blocked_bars = [
            replace(bar, suspended=True) if bar.symbol == symbol and bar.day in block_days else bar
            for bar in snapshot.bars
        ]
        block_label = f"suspend:{symbol}:{','.join(day.isoformat() for day in block_days)}"
        block_snapshot = _replace_snapshot_bars(snapshot, blocked_bars, block_label)
        block_assumption = f"基准首个真实订单{symbol}后的3个交易日设为停牌并通过共享回测引擎重跑"
    else:
        block_snapshot = _replace_snapshot_bars(snapshot, snapshot.bars, "no_created_order_to_block")
        block_assumption = "基准回测没有真实创建订单，无法构造有意义的连续不可成交证据"
    cases.append(("连续无法成交", block_assumption, block_snapshot, common, True))

    output: list[StressResult] = []
    for name, assumption, stressed_snapshot, parameters, requires_block in cases:
        result = runner(stressed_snapshot, **parameters)
        fills, blocked, pending = _result_stress_metrics(result)
        meaningful_exposure = bool(base_result.fills) and (name != "交易成本翻倍" or fills > 0)
        status = "complete" if (
            result.equity_curve and meaningful_exposure and (not requires_block or blocked > 0)
        ) else "inconclusive"
        output.append(StressResult(
            name, result.total_return, result.max_drawdown, assumption, status, True,
            fills, blocked, pending, _snapshot_hash(stressed_snapshot),
        ))
    return tuple(output)


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train_start: str
    train_end: str
    oos_start: str
    oos_end: str
    train_observations: int
    oos_observations: int
    input_last_date: str
    input_snapshot_hash: str
    input_metadata_fields: tuple[str, ...]
    latest_market_input_date: str | None
    only_past_visible: bool
    total_return: float
    max_drawdown: float
    fills: int
    pending_orders_at_end: int


@dataclass(frozen=True)
class WalkForwardEvaluation:
    status: str
    method: str
    parameter_policy: str
    folds: tuple[WalkForwardFold, ...]
    aggregate_oos_observations: int
    aggregate_total_return: float
    aggregate_max_drawdown: float
    aggregate_returns_by_date: tuple[dict[str, Any], ...]
    oos_dates_hash: str
    independent_non_overlapping: bool
    errors: tuple[str, ...]


def _metadata_through_day(metadata: Mapping[str, Any], end_day: date) -> dict[str, Any]:
    """Forward only dated market inputs visible by a fold end.

    Baselines, authorization claims, final-snapshot values and other provider
    metadata are deliberately not forwarded: they are not strategy inputs and
    may describe dates after the fold.
    """
    copied: dict[str, Any] = {}
    history = metadata.get("market_inputs_history")
    if isinstance(history, Mapping):
        copied["market_inputs_history"] = {
            str(key): value for key, value in history.items()
            if str(key)[:10] <= end_day.isoformat()
        }
    elif isinstance(history, list):
        copied["market_inputs_history"] = [
            item for item in history if isinstance(item, Mapping)
            and str(item.get("as_of") or item.get("date") or "")[:10] <= end_day.isoformat()
        ]
    return copied


def _window_returns(curve: Sequence[Mapping[str, Any]], start: str, end: str) -> list[tuple[str, float]]:
    output: list[tuple[str, float]] = []
    for previous, current in zip(curve, curve[1:]):
        current_day = str(current["date"])
        previous_equity = float(previous["equity"])
        if start <= current_day <= end and previous_equity:
            output.append((current_day, float(current["equity"]) / previous_equity - 1))
    return output


def walk_forward_oos(
    snapshot: DataSnapshot, *, oos_dates: Sequence[date | str], capital: int,
    rebalance_days: int = 10, slippage_bps: float = 8.0, fold_count: int = 3,
    runner: Callable[..., BacktestResult] = run_backtest,
) -> WalkForwardEvaluation:
    """Execute frozen parameters in expanding-history, disjoint OOS folds."""
    all_dates = sorted({bar.day for bar in snapshot.bars})
    requested = sorted({date.fromisoformat(str(day)[:10]) for day in oos_dates})
    if fold_count < 2 or len(requested) < fold_count * 2:
        return WalkForwardEvaluation(
            "incomplete", "expanding_window_walk_forward", "frozen_before_oos", (), 0, 0.0, 0.0,
            (), content_hash([day.isoformat() for day in requested]), False,
            ("insufficient OOS observations for multiple folds",),
        )
    chunks = [requested[index * len(requested) // fold_count:(index + 1) * len(requested) // fold_count]
              for index in range(fold_count)]
    folds: list[WalkForwardFold] = []
    aggregate: list[tuple[str, float]] = []
    errors: list[str] = []
    for index, window in enumerate(chunks, start=1):
        start_day, end_day = window[0], window[-1]
        train_dates = [day for day in all_dates if day < start_day]
        visible_bars = [bar for bar in snapshot.bars if bar.day <= end_day]
        if not train_dates or not visible_bars:
            errors.append(f"fold {index} has no prior history")
            continue
        fold_snapshot = DataSnapshot(
            datetime.combine(end_day, time(15, 0), tzinfo=snapshot.as_of.tzinfo),
            visible_bars, snapshot.provider, snapshot.expected_symbols,
            _metadata_through_day(snapshot.metadata, end_day),
        )
        result = runner(
            fold_snapshot, capital=capital, rebalance_days=rebalance_days,
            slippage_bps=slippage_bps,
        )
        window_returns = _window_returns(result.equity_curve, start_day.isoformat(), end_day.isoformat())
        aggregate.extend(window_returns)
        total, drawdown = _curve_metrics([value for _, value in window_returns]) if window_returns else (0.0, 0.0)
        pending = max(
            int(result.assumptions.get("pending_orders_at_end", 0)),
            int(result.equity_curve[-1].get("pending_orders", 0)) if result.equity_curve else 0,
        )
        visible_history = fold_snapshot.metadata.get("market_inputs_history")
        if isinstance(visible_history, Mapping):
            metadata_dates = [str(key)[:10] for key in visible_history]
        elif isinstance(visible_history, list):
            metadata_dates = [str(item.get("as_of") or item.get("date") or "")[:10]
                              for item in visible_history if isinstance(item, Mapping)]
        else:
            metadata_dates = []
        latest_market_input = max((value for value in metadata_dates if value), default=None)
        only_past = (
            max(bar.day for bar in fold_snapshot.bars) <= end_day
            and fold_snapshot.as_of.date() == end_day
            and (latest_market_input is None or latest_market_input <= end_day.isoformat())
            and set(fold_snapshot.metadata) <= {"market_inputs_history"}
        )
        if not window_returns:
            errors.append(f"fold {index} has no OOS return observations")
        folds.append(WalkForwardFold(
            index, all_dates[0].isoformat(), train_dates[-1].isoformat(),
            start_day.isoformat(), end_day.isoformat(), len(train_dates), len(window_returns),
            max(bar.day for bar in fold_snapshot.bars).isoformat(), _snapshot_hash(fold_snapshot),
            tuple(sorted(fold_snapshot.metadata)), latest_market_input, only_past, total, drawdown,
            sum(start_day <= fill.fill_day <= end_day for fill in result.fills), pending,
        ))
    return_dates = [day for day, _ in aggregate]
    independent = len(return_dates) == len(set(return_dates)) and all(
        left.oos_end < right.oos_start for left, right in zip(folds, folds[1:])
    )
    aggregate_total, aggregate_drawdown = _curve_metrics([value for _, value in aggregate]) if aggregate else (0.0, 0.0)
    complete = (
        len(folds) == fold_count and not errors and independent
        and all(fold.only_past_visible and fold.oos_observations > 0 for fold in folds)
    )
    return WalkForwardEvaluation(
        "complete" if complete else "incomplete", "expanding_window_walk_forward",
        "parameters frozen before first OOS fold; no refit required by deterministic rule strategy",
        tuple(folds), len(aggregate), aggregate_total, aggregate_drawdown,
        tuple({"date": day, "return": round(value, 10)} for day, value in aggregate),
        content_hash(return_dates), independent, tuple(errors),
    )


@dataclass(frozen=True)
class TradeStatistics:
    status: str
    completed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float | None
    gross_profit: float
    gross_loss: float
    profit_loss_ratio: float | None
    profit_loss_ratio_status: str
    profit_loss_ratio_method: str
    median_holding_trading_days: float | None
    traded_notional: float
    turnover: float | None
    turnover_method: str
    reason: str | None


@dataclass(frozen=True)
class ContributionDimension:
    status: str
    method: str
    contributions: dict[str, float]
    absolute_contribution_denominator: float
    max_absolute_share: float | None
    reason: str | None


@dataclass(frozen=True)
class AttributionEvaluation:
    status: str
    evaluation_start: str
    evaluation_end: str
    year: ContributionDimension
    theme: ContributionDimension
    stock: ContributionDimension
    errors: tuple[str, ...]


def _item_value(item: Any, field: str) -> Any:
    if isinstance(item, Mapping):
        return item[field]
    return getattr(item, field)


def evaluate_trade_statistics(
    fills: Sequence[Any], equity_curve: Sequence[Mapping[str, Any]], *, evaluation_start: str | None = None,
) -> TradeStatistics:
    """Calculate realized-trade and turnover evidence from the actual ledger.

    Buy fills before ``evaluation_start`` are retained as cost basis for a sell
    inside the evaluation window.  This avoids treating the final-test boundary
    as a free acquisition while keeping reported outcomes tied to real fills.
    Holding duration is counted on the backtest's trading-day curve, not by
    calendar-day subtraction.
    """
    curve = [dict(item) for item in equity_curve if evaluation_start is None or str(item["date"]) >= evaluation_start]
    day_index = {str(item["date"]): index for index, item in enumerate(equity_curve)}
    positions: dict[str, dict[str, Any]] = {}
    realized: list[float] = []
    holding_days: list[int] = []
    traded_notional = 0.0
    for fill in sorted(fills, key=lambda item: (str(_item_value(item, "fill_day")), str(_item_value(item, "symbol")), str(_item_value(item, "side")))):
        day_value = _item_value(fill, "fill_day")
        fill_day = day_value.isoformat() if isinstance(day_value, date) else str(day_value)
        symbol = str(_item_value(fill, "symbol"))
        side = str(_item_value(fill, "side"))
        shares = int(_item_value(fill, "shares"))
        price = float(_item_value(fill, "price"))
        fee = float(_item_value(fill, "fee"))
        if evaluation_start is None or fill_day >= evaluation_start:
            traded_notional += shares * price
        if side == "buy":
            state = positions.setdefault(symbol, {"shares": 0, "cost": 0.0, "entry_day": fill_day})
            if not state["shares"]:
                state["entry_day"] = fill_day
            state["shares"] += shares
            state["cost"] += shares * price + fee
            continue
        state = positions.get(symbol)
        if side != "sell" or not state or state["shares"] <= 0:
            continue
        sold = min(shares, int(state["shares"]))
        allocated_cost = float(state["cost"]) * sold / int(state["shares"])
        pnl = sold * price - fee - allocated_cost
        if evaluation_start is None or fill_day >= evaluation_start:
            realized.append(pnl)
            if state["entry_day"] in day_index and fill_day in day_index:
                holding_days.append(max(1, day_index[fill_day] - day_index[state["entry_day"]]))
        state["cost"] -= allocated_cost
        state["shares"] -= sold
        if state["shares"] <= 0:
            positions.pop(symbol, None)
    gross_profit = sum(value for value in realized if value > 0)
    gross_loss = abs(sum(value for value in realized if value < 0))
    average_equity = mean(float(item["equity"]) for item in curve) if curve else 0.0
    turnover = traded_notional / average_equity if average_equity > 0 else None
    if not realized:
        return TradeStatistics(
            "unavailable", 0, 0, 0, None, 0.0, 0.0, None, "no_completed_trades",
            "平均盈利交易损益/平均亏损交易损益绝对值",
            None, round(traded_notional, 2), round(turnover, 6) if turnover is not None else None,
            "成交额合计/评估期平均权益", "评估期没有已完成的买卖回合，胜率与盈亏比不可计算",
        )
    wins = sum(value > 0 for value in realized)
    losses = sum(value < 0 for value in realized)
    average_profit = gross_profit / wins if wins else 0.0
    average_loss = gross_loss / losses if losses else 0.0
    ratio = average_profit / average_loss if average_loss > 0 else None
    return TradeStatistics(
        "available", len(realized), wins, losses, round(wins / len(realized), 6),
        round(gross_profit, 2), round(gross_loss, 2), round(ratio, 6) if ratio is not None else None,
        "available" if ratio is not None else "no_losing_trades",
        "平均盈利交易损益/平均亏损交易损益绝对值",
        float(median(holding_days)) if holding_days else None,
        round(traded_notional, 2), round(turnover, 6) if turnover is not None else None,
        "成交额合计/评估期平均权益", None,
    )


def _contribution_dimension(values: Mapping[str, float], method: str, *, unavailable_reason: str) -> ContributionDimension:
    cleaned = {str(key): round(float(value), 6) for key, value in sorted(values.items())
               if isfinite(float(value)) and abs(float(value)) > 1e-9}
    denominator = sum(abs(value) for value in cleaned.values())
    if denominator <= 1e-9:
        return ContributionDimension("unavailable", method, cleaned, 0.0, None, unavailable_reason)
    maximum = max(abs(value) for value in cleaned.values()) / denominator
    return ContributionDimension("available", method, cleaned, round(denominator, 6), round(maximum, 6), None)


def evaluate_contribution_attribution(
    result: BacktestResult, snapshot: DataSnapshot, *, evaluation_start: str,
) -> AttributionEvaluation:
    """Attribute final-test P&L without manufacturing missing evidence.

    Year contribution uses observed changes in the backtest equity curve.
    Stock contribution uses actual fill cash flows plus marked opening/ending
    holdings.  Theme contribution aggregates those stock results using the
    latest PIT theme visible inside the evaluation window.  Any missing price
    or theme makes that dimension unavailable and therefore fail closed.
    """
    curve = [dict(item) for item in result.equity_curve if str(item["date"]) >= evaluation_start]
    if not curve:
        unavailable = ContributionDimension("unavailable", "no final-test curve", {}, 0.0, None,
                                            "最终测试权益曲线为空")
        return AttributionEvaluation("unavailable", evaluation_start, evaluation_start,
                                     unavailable, unavailable, unavailable, ("final_test_curve_empty",))
    start_day, end_day = str(curve[0]["date"]), str(curve[-1]["date"])
    year_values: dict[str, float] = defaultdict(float)
    for previous, current in zip(curve, curve[1:]):
        year_values[str(current["date"])[:4]] += float(current["equity"]) - float(previous["equity"])
    year = _contribution_dimension(
        year_values, "逐日权益变化按交易日年份汇总；集中度分母为各年贡献绝对值之和",
        unavailable_reason="最终测试权益没有可归因变化",
    )

    fills = sorted(result.fills, key=lambda item: (item.fill_day, item.symbol, item.side))
    opening_shares: dict[str, int] = defaultdict(int)
    stock_values: dict[str, float] = defaultdict(float)
    errors: list[str] = []
    for fill in fills:
        day = fill.fill_day.isoformat()
        # The first curve point is end-of-day equity, so same-day fills are
        # already embedded in the opening position and must not be counted as
        # evaluation-period cash flows (especially their fees) a second time.
        if day <= start_day:
            opening_shares[fill.symbol] += fill.shares if fill.side == "buy" else -fill.shares
        elif day <= end_day:
            cash_flow = fill.price * fill.shares
            stock_values[fill.symbol] += (-cash_flow - fill.fee) if fill.side == "buy" else (cash_flow - fill.fee)

    histories: dict[str, list[Any]] = defaultdict(list)
    for bar in snapshot.bars:
        histories[bar.symbol].append(bar)
    for values in histories.values():
        values.sort(key=lambda item: item.day)

    def visible_bar(symbol: str, day: str) -> Any | None:
        target = date.fromisoformat(day)
        return next((bar for bar in reversed(histories.get(symbol, [])) if bar.day <= target), None)

    current_shares = dict(opening_shares)
    for symbol, shares in opening_shares.items():
        if shares <= 0:
            continue
        bar = visible_bar(symbol, start_day)
        if bar is None:
            errors.append(f"opening_price_missing:{symbol}")
        else:
            stock_values[symbol] -= shares * float(bar.close)
    for fill in fills:
        day = fill.fill_day.isoformat()
        if start_day < day <= end_day:
            current_shares[fill.symbol] = current_shares.get(fill.symbol, 0) + (fill.shares if fill.side == "buy" else -fill.shares)
    for symbol, shares in current_shares.items():
        if shares <= 0:
            continue
        bar = visible_bar(symbol, end_day)
        if bar is None:
            errors.append(f"ending_price_missing:{symbol}")
        else:
            stock_values[symbol] += shares * float(bar.close)
    equity_change = float(curve[-1]["equity"]) - float(curve[0]["equity"])
    stock_change = sum(stock_values.values())
    evaluated_fills = [fill for fill in fills if start_day < fill.fill_day.isoformat() <= end_day]
    # Fill prices are stored to two decimals while the simulator updates cash
    # with the pre-rounded execution price.  Bound that known ledger rounding
    # error explicitly rather than accepting an arbitrary percentage mismatch.
    reconciliation_tolerance = max(
        1.0, sum(fill.shares * .005 + .01 for fill in evaluated_fills) + .02,
    )
    if abs(stock_change - equity_change) > reconciliation_tolerance:
        errors.append(
            f"stock_equity_reconciliation_failed:stock={stock_change:.6f}:equity={equity_change:.6f}:"
            f"tolerance={reconciliation_tolerance:.6f}"
        )
    stock = _contribution_dimension(
        {} if errors else stock_values,
        "期初持仓市值 + 评估期真实成交现金流/费用 + 期末持仓市值；集中度使用绝对贡献",
        unavailable_reason="没有可归因股票损益" if not errors else "；".join(errors),
    )
    theme_values: dict[str, float] = defaultdict(float)
    if stock.status == "available":
        for symbol, contribution in stock.contributions.items():
            bar = visible_bar(symbol, end_day)
            if bar is None or not str(bar.theme).strip():
                errors.append(f"theme_missing:{symbol}")
                continue
            theme_values[str(bar.theme)] += contribution
    theme = _contribution_dimension(
        {} if errors else theme_values,
        "股票实际损益按最终测试期末可见PIT题材汇总；集中度使用绝对贡献",
        unavailable_reason="没有可归因题材损益" if not errors else "；".join(errors),
    )
    status = "available" if all(item.status == "available" for item in (year, theme, stock)) else "unavailable"
    return AttributionEvaluation(status, start_day, end_day, year, theme, stock, tuple(errors))


@dataclass(frozen=True)
class GateInput:
    oos_max_drawdown: float
    average_holdings: float
    median_holding_days: float
    after_cost_excess_return: float | None
    max_year_contribution: float | None
    max_theme_contribution: float | None
    max_stock_contribution: float | None
    neighbor_stability_ratio: float
    baseline_count: int
    capacity_tier_count: int
    capacity_executable: tuple[bool, ...]
    walk_forward_oos_complete: bool
    stress_evidence_complete: bool
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
    contribution_values = (
        metrics.max_year_contribution, metrics.max_theme_contribution, metrics.max_stock_contribution,
    )
    contribution_available = all(value is not None and isfinite(value) for value in contribution_values)
    contribution_actual = {
        "year": metrics.max_year_contribution,
        "theme": metrics.max_theme_contribution,
        "stock": metrics.max_stock_contribution,
    }
    gates = (
        gate("BT-008-DRAWDOWN-TARGET", metrics.oos_max_drawdown >= -.15, metrics.oos_max_drawdown, "样本外最大回撤目标不超过15%", "优化目标；不是风险保证"),
        gate("BT-008-DRAWDOWN-HARD", metrics.oos_max_drawdown >= -.18, metrics.oos_max_drawdown, "样本外最大回撤不得超过18%"),
        gate("BT-008-HOLDINGS", 3 <= metrics.average_holdings <= 5, metrics.average_holdings, "平均持仓3至5只"),
        gate("BT-008-HOLDING-DAYS", 40 <= metrics.median_holding_days <= 80, metrics.median_holding_days, "持仓中位数40至80个交易日"),
        gate("BT-008-EXCESS", metrics.after_cost_excess_return is not None and metrics.after_cost_excess_return > 0,
             metrics.after_cost_excess_return, "扣费后样本外超额收益为正",
             "缺少显式中证全指基线，无法计算超额收益" if metrics.after_cost_excess_return is None else ""),
        gate("BT-008-CONCENTRATION", contribution_available and max(float(value) for value in contribution_values if value is not None) <= .50,
             contribution_actual, "单一年份、题材或股票贡献均不超过50%",
             "最终测试贡献归因缺失或不可计算，禁止用固定占位值通过门禁" if not contribution_available else ""),
        gate("BT-008-STABILITY", metrics.neighbor_stability_ratio >= .67, metrics.neighbor_stability_ratio, "至少三分之二邻近参数保持稳定"),
        gate("BT-005-WALK-FORWARD", metrics.walk_forward_oos_complete, metrics.walk_forward_oos_complete,
             "冻结参数后至少三折、仅使用过去数据的独立滚动样本外回放",
             "缺少完整多折走步OOS证据" if not metrics.walk_forward_oos_complete else ""),
        gate("BT-005-STRESS", metrics.stress_evidence_complete, metrics.stress_evidence_complete,
             "压力场景必须使用共享回测引擎重跑且证据完整",
             "压力场景缺失、未由引擎重跑或连续不可成交场景未产生真实阻塞" if not metrics.stress_evidence_complete else ""),
        gate("BT-005-FINAL-TEST", metrics.final_test_isolated, metrics.final_test_isolated, "最终测试集未参与调参"),
        gate("BT-006-BASELINES", metrics.baseline_count >= 3, metrics.baseline_count, "沪深300、中证全指、简单动量三基线",
             "快照缺少完整、逐日对齐的显式基线序列" if metrics.baseline_count < 3 else ""),
        gate("BT-007-CAPACITY", metrics.capacity_tier_count == 4 and len(metrics.capacity_executable) == 4
             and all(metrics.capacity_executable),
             {"tiers": metrics.capacity_tier_count, "executable": list(metrics.capacity_executable)},
             "10万、100万、300万、1000万四档均有真实成交且无期末未成交订单",
             "档位数量齐全但不能替代逐档可成交性证据" if not all(metrics.capacity_executable) else ""),
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
                           report: Mapping[str, Any], *,
                           additional_artifacts: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Write canonical artifacts; refuse to mutate an existing evidence directory."""
    target = Path(output_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"evidence directory is immutable and already populated: {target}")
    target.mkdir(parents=True, exist_ok=True)
    artifacts = {"manifest.json": manifest, "gates.json": gates, "research_report.json": report}
    for name, payload in (additional_artifacts or {}).items():
        if not name.endswith(".json") or Path(name).name != name:
            raise ValueError("research artifact names must be flat .json filenames")
        if name in artifacts or name == "hashes.json":
            raise ValueError(f"duplicate or reserved research artifact name: {name}")
        artifacts[name] = payload
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
