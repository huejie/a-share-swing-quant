from __future__ import annotations
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo
from .models import DataSnapshot, QualityIssue, QualityReport
from .pit import parse_time


DEFAULT_DATASET_MAX_AGE_HOURS = {
    "bars": 72.0, "security_master": 168.0, "theme_memberships": 168.0,
    "corporate_actions": 168.0, "financials": 24 * 120.0, "announcements": 72.0,
    "market_funding": 72.0, "global_risk": 72.0,
}

LICENSED_REQUIRED_DATASETS = {
    "bars", "security_master", "theme_memberships", "corporate_actions",
    "financials", "announcements", "market_funding", "global_risk",
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
    invalid = [b.symbol for b in snapshot.bars if b.low > min(b.open,b.close) or b.high < max(b.open,b.close)
               or min(b.open,b.high,b.low,b.close)<=0 or b.amount < 0 or b.volume < 0 or b.adj_factor<=0]
    if invalid: issues.append(QualityIssue("INVALID_OHLC", "error", f"价格或成交额异常：{', '.join(sorted(set(invalid))[:3])}"))
    keys=[(bar.symbol,bar.day) for bar in snapshot.bars]
    if len(keys)!=len(set(keys)):
        issues.append(QualityIssue("DUPLICATE_BAR","error","存在重复的证券交易日行情记录"))
    future=sorted({bar.day.isoformat() for bar in snapshot.bars if bar.day>as_of.date()})
    if future:
        issues.append(QualityIssue("FUTURE_BAR","error",f"行情日期晚于决策时点：{future[0]}"))
    state_conflicts=sorted({bar.symbol for bar in snapshot.bars if
                            (bar.suspended and (bar.volume>0 or bar.amount>0)) or
                            (bar.limit_up and bar.limit_down)})
    if state_conflicts:
        issues.append(QualityIssue("TRADE_STATE_INCONSISTENT","error",
                                   f"停牌/涨跌停状态与行情矛盾：{', '.join(state_conflicts[:3])}"))
    by_day:dict={}
    for bar in snapshot.bars:
        summary=by_day.setdefault(bar.day,{"symbols":set(),"amount":0.0})
        summary["symbols"].add(bar.symbol);summary["amount"]+=bar.amount
    ordered_days=sorted(by_day)
    if len(ordered_days)>=6:
        reference=ordered_days[-21:-1]
        count_median=median(len(by_day[day]["symbols"]) for day in reference)
        amount_median=median(by_day[day]["amount"] for day in reference)
        latest=by_day[ordered_days[-1]]
        if count_median and not .5<=len(latest["symbols"])/count_median<=1.5:
            issues.append(QualityIssue("SECURITY_COUNT_ANOMALY","error",
                                       "最新交易日证券数量相对近期中位数异常"))
        if amount_median and not .05<=latest["amount"]/amount_median<=20:
            issues.append(QualityIssue("TURNOVER_ANOMALY","error","最新交易日总成交额相对近期中位数异常"))
    by_symbol:dict={}
    for bar in snapshot.bars:by_symbol.setdefault(bar.symbol,[]).append(bar)
    factor_jumps=[]
    for symbol,bars in by_symbol.items():
        values=[bar.adj_factor for bar in sorted(bars,key=lambda item:item.day)]
        if any(max(a,b)/min(a,b)>20 for a,b in zip(values,values[1:])):factor_jumps.append(symbol)
    if factor_jumps:
        issues.append(QualityIssue("ADJ_FACTOR_JUMP","error",
                                   f"复权因子出现无法解释的极端跳变：{', '.join(sorted(factor_jumps)[:3])}"))
    expected_sessions=snapshot.metadata.get("expected_trading_days") if isinstance(snapshot.metadata,dict) else None
    if isinstance(expected_sessions,list):
        expected={str(day) for day in expected_sessions if str(day)<=as_of.date().isoformat()}
        present={day.isoformat() for day in ordered_days}
        missing=sorted(expected-present)
        if missing:
            issues.append(QualityIssue("MISSING_TRADING_SESSION","error",
                                       f"缺少声明的交易日行情：{', '.join(missing[:3])}"))
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
    if snapshot.provider == "licensed-csv-bundle":
        declared = set(datasets) if isinstance(datasets, dict) else set()
        missing_required = sorted(LICENSED_REQUIRED_DATASETS - declared)
        if missing_required:
            issues.append(QualityIssue("LICENSED_DATASET_CONTRACT", "error",
                                       f"授权数据包缺少必需数据集：{', '.join(missing_required)}"))
        materialization = metadata.get("pit_materialization")
        if not isinstance(materialization, dict) or materialization.get("status") != "complete":
            issues.append(QualityIssue("PIT_MATERIALIZATION_INCOMPLETE", "error",
                                       "财务、公告、公司行为或市场/全球记录未完整物化到决策时点"))
        history = metadata.get("market_inputs_history")
        if not isinstance(history, (dict, list)) or not history:
            issues.append(QualityIssue("MARKET_INPUT_HISTORY_MISSING", "error",
                                       "缺少可按历史时点选择的A股资金/估值与全球风险输入"))
        else:
            history_days = set(history) if isinstance(history, dict) else {
                str(item.get("date") or item.get("as_of"))[:10] for item in history if isinstance(item, dict)
            }
            bar_days = {bar.day.isoformat() for bar in snapshot.bars}
            missing_history = sorted(bar_days - history_days)
            if missing_history:
                issues.append(QualityIssue("MARKET_INPUT_HISTORY_INCOMPLETE", "error",
                                           f"历史市场输入未覆盖行情交易日：{', '.join(missing_history[:3])}"))
            if isinstance(history, dict) and any(
                not isinstance(item, dict) or
                set(item.get("market_funding_components", {})) != {
                    "margin_balance", "margin_balance_change", "etf_share_change", "market_breadth"
                } or set(item.get("global_risk_components", {})) != {
                    "global_equity", "usd_cny", "interest_rate", "volatility_index", "commodity_index"
                }
                for item in history.values()
            ):
                issues.append(QualityIssue("MARKET_INPUT_COMPONENTS_INCOMPLETE", "error",
                                           "融资、ETF、市场广度或全球五类风险分项不完整"))
        if any(bar.free_float_market_cap <= 0 for bar in snapshot.bars):
            issues.append(QualityIssue("FREE_FLOAT_MARKET_CAP_MISSING", "error",
                                       "授权行情缺少正数流通市值"))
        if any(bar.listed_days < 1 or bar.share_multiplier <= 0 or bar.cash_dividend_per_share < 0
               for bar in snapshot.bars):
            issues.append(QualityIssue("SECURITY_OR_ACTION_STATE_INVALID", "error",
                                       "上市交易日龄、累计股本倍数或累计现金分红状态无效"))
        visible_records=metadata.get("pit_records_visible")
        if not isinstance(visible_records,list) or not visible_records or any(
            not isinstance(item,dict) or not item.get("source_ref") or
            not item.get("parser_version") or not item.get("collected_at")
            for item in visible_records
        ):
            issues.append(QualityIssue("PIT_LINEAGE_INCOMPLETE", "error",
                                       "PIT记录缺少来源、采集时间或解析版本"))
    enrichments=metadata.get("enrichments")
    if isinstance(enrichments,dict):
        adjustment=enrichments.get("adj_factor")
        if not isinstance(adjustment,dict) or adjustment.get("status")!="available":
            issues.append(QualityIssue("ENRICHMENT_REQUIRED:adj_factor","error","复权因子不完整，禁止用原始价格生成观察或生产建议"))
        if snapshot.provider in {"tushare", "akshare"}:
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
