from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class MarketRegime(StrEnum):
    STRONG = "强势"
    BULLISH_RANGE = "震荡偏强"
    BEARISH_RANGE = "震荡偏弱"
    DECLINE = "下跌"
    EXTREME_RISK = "极端风险"


class Lifecycle(StrEnum):
    DORMANT = "潜伏"
    STARTING = "启动"
    EXPANDING = "扩散"
    HEALTHY = "健康趋势"
    ACCELERATING = "加速"
    CROWDED = "拥挤"
    FADING = "退潮"


@dataclass(frozen=True)
class Bar:
    symbol: str
    name: str
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    theme: str
    industry: str
    board: str = "主板"
    is_st: bool = False
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False
    listed_days: int = 1000
    quality: float = 70.0
    catalyst: float = 60.0
    is_delisting: bool = False
    regulatory_risk: bool = False
    audit_abnormal: bool = False
    event_risk: bool = False
    adj_factor: float = 1.0


@dataclass
class DataSnapshot:
    as_of: datetime
    bars: list[Bar]
    provider: str
    expected_symbols: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str


@dataclass
class QualityReport:
    status: str
    freshness: str
    as_of: datetime
    age_hours: float
    issues: list[QualityIssue]


@dataclass(frozen=True)
class MarketAssessment:
    score: float
    regime: MarketRegime
    exposure_cap: float
    components: dict[str, float]
    style: str
    reasons: tuple[str, ...]
    completeness: float = 1.0
    confidence: float = 1.0
    data_timestamp: datetime | None = None
    component_quality: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ThemeAssessment:
    name: str
    score: float
    lifecycle: Lifecycle
    relative_strength: float
    breadth: float
    turnover: float
    fundamental: float
    catalyst: float
    leadership: float
    crowding: float
    lifecycle_reason: str = ""
    fund_flow_label: str = "成交额占比变化代理"


@dataclass(frozen=True)
class StockAssessment:
    symbol: str
    name: str
    theme: str
    industry: str
    score: float
    close: float
    atr_pct: float
    avg_amount_20d: float
    relative_strength: float
    trend: float
    reasons: tuple[str, ...]
    eligible: bool
    excluded_reason: str | None = None
    theme_lifecycle: str = ""
    gate_results: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PositionAdvice:
    symbol: str
    name: str
    theme: str
    action: str
    entry_state: str
    trigger_zone: tuple[float, float]
    target_weight: float
    initial_weight: float
    current_weight: float
    entry_price: float
    initial_stop: float
    protective_price: float | None
    highest_price: float
    score: float
    thesis: tuple[str, ...]
    invalidation: str
    risk_notes: tuple[str, ...]
    expected_holding_days: tuple[int, int]
    next_review_at: datetime
    model_version: str
    data_timestamp: datetime
    entry_at: datetime | None = None
    exit_priority: int | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    priority: int | None
    reason: str
    protective_price: float | None


def jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value
