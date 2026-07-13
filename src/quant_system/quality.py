from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
from .models import DataSnapshot, QualityIssue, QualityReport
from .pit import parse_time


DEFAULT_DATASET_MAX_AGE_HOURS = {
    "bars": 72.0, "security_master": 168.0, "theme_memberships": 168.0,
    "financials": 24 * 120.0, "announcements": 72.0, "global_risk": 72.0,
}


def check_quality(snapshot: DataSnapshot, now: datetime | None = None, max_age_hours: float = 72) -> QualityReport:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    as_of = snapshot.as_of if snapshot.as_of.tzinfo else snapshot.as_of.replace(tzinfo=now.tzinfo)
    age = max(0.0, (now - as_of).total_seconds() / 3600)
    issues: list[QualityIssue] = []
    symbols = {b.symbol for b in snapshot.bars if b.day == max(x.day for x in snapshot.bars)} if snapshot.bars else set()
    if not snapshot.bars: issues.append(QualityIssue("NO_DATA", "error", "没有可用行情，停止生成建议"))
    if snapshot.expected_symbols and len(symbols) < snapshot.expected_symbols * .8:
        issues.append(QualityIssue("LOW_COVERAGE", "error", f"最新交易日仅覆盖 {len(symbols)}/{snapshot.expected_symbols} 只股票"))
    invalid = [b.symbol for b in snapshot.bars if b.low > min(b.open,b.close) or b.high < max(b.open,b.close) or b.amount < 0]
    if invalid: issues.append(QualityIssue("INVALID_OHLC", "error", f"价格或成交额异常：{', '.join(sorted(set(invalid))[:3])}"))
    freshness = "fresh" if age <= max_age_hours else "stale"
    if freshness == "stale": issues.append(QualityIssue("STALE", "error", f"数据距今 {age:.1f} 小时，禁止发布新建议"))
    datasets = snapshot.metadata.get("datasets") if isinstance(snapshot.metadata, dict) else None
    if isinstance(datasets, dict):
        for name, details in sorted(datasets.items()):
            if not isinstance(details, dict):
                issues.append(QualityIssue(f"DATASET_METADATA_INVALID:{name}", "error", f"数据集 {name} 缺少可解析的新鲜度元数据")); continue
            required = details.get("required", True) is not False
            dataset_as_of = details.get("as_of")
            if not dataset_as_of:
                if required:
                    issues.append(QualityIssue(f"DATASET_ASOF_MISSING:{name}", "error", f"必需数据集 {name} 未声明 as_of"))
                else:
                    issues.append(QualityIssue(f"DATASET_DEGRADED:{name}", "warning", f"可选数据集 {name} 缺失或未声明 as_of，相关分项必须降级"))
                continue
            try: dataset_time = parse_time(dataset_as_of)
            except (TypeError, ValueError):
                issues.append(QualityIssue(f"DATASET_ASOF_INVALID:{name}", "error", f"数据集 {name} 的 as_of 无法解析")); continue
            dataset_age = max(0.0, (now - dataset_time.astimezone(now.tzinfo)).total_seconds()/3600)
            allowed = float(details.get("max_age_hours", DEFAULT_DATASET_MAX_AGE_HOURS.get(name, max_age_hours)))
            if required and dataset_age > allowed:
                freshness = "stale"
                issues.append(QualityIssue(f"DATASET_STALE:{name}", "error", f"必需数据集 {name} 已过期 {dataset_age:.1f} 小时（上限 {allowed:.1f}）"))
            elif not required and dataset_age > allowed:
                issues.append(QualityIssue(f"DATASET_DEGRADED:{name}", "warning", f"可选数据集 {name} 已过期，相关分项必须降级"))
    metadata=snapshot.metadata if isinstance(snapshot.metadata,dict) else {}
    enrichments=metadata.get("enrichments")
    if isinstance(enrichments,dict):
        adjustment=enrichments.get("adj_factor")
        if not isinstance(adjustment,dict) or adjustment.get("status")!="available":
            issues.append(QualityIssue("ENRICHMENT_REQUIRED:adj_factor","error","复权因子不完整，禁止用原始价格生成观察或生产建议"))
        for name in ("daily_basic","index_daily"):
            details=enrichments.get(name)
            if not isinstance(details,dict) or details.get("status")!="available":
                issues.append(QualityIssue(f"ENRICHMENT_DEGRADED:{name}","warning",f"可选增强数据 {name} 不可用，相关分项已降级为中性"))
    elif snapshot.provider in {"tushare","akshare"}:
        # Public adapters must explicitly prove adjustment availability before
        # an observation-only path may use their prices.
        issues.append(QualityIssue("ENRICHMENT_REQUIRED:adj_factor","error","公开数据源未声明复权因子完整性，禁止生成观察建议"))
    if snapshot.bars:
        latest_day=max(bar.day for bar in snapshot.bars)
        latest_bars=[bar for bar in snapshot.bars if bar.day==latest_day]
        placeholders={"","未配置","行业未分类","未分类"}
        if latest_bars and all(bar.theme.strip() in placeholders or bar.industry.strip() in placeholders for bar in latest_bars):
            issues.append(QualityIssue("GROUPING_UNAVAILABLE","error","行业/题材分组全部缺失，无法满足组合分散约束"))
    if metadata.get("production_ready") is False and snapshot.provider != "deterministic-demo":
        issues.append(QualityIssue("NOT_PRODUCTION_READY", "error", "数据源未同时通过授权、PIT 和完整性验证，禁止发布生产建议"))
    status = "blocked" if any(i.severity == "error" for i in issues) else ("warning" if issues else "healthy")
    return QualityReport(status, freshness, as_of, round(age, 1), issues)
