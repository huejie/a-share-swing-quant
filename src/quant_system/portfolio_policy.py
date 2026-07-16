"""Shared stateful model-portfolio policy for production EOD and backtests."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from .engine import MODEL_VERSION, build_portfolio, evaluate_exit


def weekly_theme_names(snapshot, themes: list, previous_decision: dict | None = None) -> list[str]:
    """Freeze the 3--4 core themes inside an ISO week for every strategy caller."""
    if previous_decision:
        try:
            previous_week = datetime.fromisoformat(previous_decision["data_timestamp"]).date().isocalendar()[:2]
        except (KeyError, TypeError, ValueError):
            previous_week = None
        stored = previous_decision.get("snapshot", {}).get("selected_theme_names", [])
        if (previous_decision.get("model_version") == MODEL_VERSION and
                previous_week == snapshot.as_of.date().isocalendar()[:2] and stored):
            return list(stored)[:4]
    allowed = {"启动", "扩散", "健康趋势"}
    return [theme.name for theme in themes if theme.lifecycle.value in allowed][:4]


def buffered_portfolio(
    snapshot,
    market,
    stocks: list,
    *,
    previous_decision: dict | None,
    capital: float,
    target_count: int,
    risk_per_trade: float,
    max_adv_participation: float,
    max_drawdown: float,
    portfolio_drawdown: float = 0.0,
    active_factors: frozenset[str] | None = None,
    portfolio_builder: Callable = build_portfolio,
):
    """Apply the shared one-normal-replacement-per-week portfolio state machine.

    The caller owns persistence.  All state needed for deterministic replay is
    supplied explicitly through ``previous_decision`` and scalar settings.
    """
    previous = previous_decision
    previous_snapshot = previous.get("snapshot", {}) if previous else {}
    old_symbols = [position["symbol"] for position in previous_snapshot.get("portfolio", [])]
    if previous and previous.get("model_version") != MODEL_VERSION:
        old_symbols = []
        previous = None
        previous_snapshot = {}

    current_week = snapshot.as_of.date().isocalendar()[:2]
    previous_week = None
    if previous:
        try:
            previous_week = datetime.fromisoformat(previous["data_timestamp"]).date().isocalendar()[:2]
        except (KeyError, TypeError, ValueError):
            previous_week = None
    prior_turnover = previous.get("turnover", {}) if previous else {}
    week_used = (int(prior_turnover.get("week_normal_replacements_used", 0))
                 if previous_week == current_week else 0)
    replacement_budget = max(0, 1 - week_used)

    by_symbol = {stock.symbol: stock for stock in stocks}
    eligible = [stock for stock in stocks if stock.eligible]
    histories = {}
    for bar in snapshot.bars:
        histories.setdefault(bar.symbol, []).append(bar)
    for values in histories.values():
        values.sort(key=lambda item: item.day)
    previous_positions = {item["symbol"]: item for item in previous_snapshot.get("portfolio", [])}
    position_states = {}
    risk_exits = []
    rank = {stock.symbol: (index + 1) / max(1, len(eligible)) for index, stock in enumerate(eligible)}
    hard = []
    normal_candidates = []
    retained = []

    for symbol in old_symbols:
        stock = by_symbol.get(symbol)
        prior = previous_positions.get(symbol, {})
        symbol_bars = histories.get(symbol, [])
        if stock is None or not symbol_bars:
            hard.append({"symbol": symbol, "reason": "当前证券池或行情缺失", "kind": "hard_risk"})
            continue
        if not stock.eligible:
            hard.append({"symbol": symbol,
                         "reason": stock.excluded_reason or "当前证券硬风险门禁失败",
                         "kind": "hard_risk"})
            continue

        latest = symbol_bars[-1]
        current_factor = float(latest.adj_factor or 1.0)
        prior_factor = float(prior.get("reference_adj_factor") or current_factor)
        corporate_action_ratio = prior_factor / current_factor if current_factor > 0 else 1.0
        prior_entry = prior.get("entry_price")
        entry = float(prior_entry) * corporate_action_ratio if prior_entry is not None else latest.close
        prior_peak = prior.get("highest_price")
        peak = max(float(prior_peak) * corporate_action_ratio if prior_peak is not None else entry, latest.high)
        prior_stop = prior.get("initial_stop")
        initial_stop = float(prior_stop) * corporate_action_ratio if prior_stop is not None else entry * .90
        raw_entry_at = prior.get("entry_at") or prior.get("data_timestamp") or snapshot.as_of.isoformat()
        try:
            entry_at = datetime.fromisoformat(str(raw_entry_at))
        except ValueError:
            entry_at = snapshot.as_of
        holding_days = sum(1 for bar in symbol_bars if bar.day >= entry_at.date())

        market_returns = []
        for peer_bars in histories.values():
            period = [bar for bar in peer_bars if bar.day >= entry_at.date()]
            if len(period) < 2:
                continue
            start, end = period[0], period[-1]
            adjusted_start = (start.close * (start.adj_factor / end.adj_factor)
                              if end.adj_factor > 0 else start.close)
            if adjusted_start > 0:
                market_returns.append(end.close / adjusted_start - 1)
        market_return = sum(market_returns) / len(market_returns) if market_returns else None
        position_return = latest.close / entry - 1 if entry > 0 else None
        excess_return = (position_return - market_return
                         if position_return is not None and market_return is not None else None)
        exit_decision = evaluate_exit(
            entry=entry, peak=peak, close=latest.close, initial_stop=initial_stop,
            holding_days=holding_days, hard_risk=not stock.eligible,
            portfolio_drawdown=portfolio_drawdown,
            extreme_market=market.exposure_cap <= 0,
            theme_fading=stock.theme_lifecycle == "退潮", trend_broken=stock.trend < 35,
            previous_protective=prior.get("protective_price"),
            corporate_action_price_ratio=corporate_action_ratio,
            max_portfolio_drawdown=max_drawdown, excess_return=excess_return,
        )
        position_states[symbol] = {
            "entry_price": entry, "highest_price": peak, "initial_stop": initial_stop,
            "protective_price": exit_decision.protective_price, "entry_at": entry_at,
            "holding_days": holding_days,
            "entry_state": prior.get("entry_state", "持仓复核"),
        }
        if exit_decision.should_exit:
            risk_exits.append({"symbol": symbol, "reason": exit_decision.reason,
                               "kind": "risk_exit", "priority": exit_decision.priority})
        elif rank.get(symbol, 1) > .30:
            normal_candidates.append({"symbol": symbol,
                                      "reason": f"当前排名 {rank[symbol] * 100:.1f}%，跌出前30%",
                                      "kind": "normal"})
        else:
            retained.append(symbol)

    if portfolio_drawdown <= -abs(max_drawdown):
        replaced = [{"symbol": symbol, "reason": f"组合达到{abs(max_drawdown) * 100:.0f}%硬风控",
                     "kind": "risk_exit", "priority": 2} for symbol in old_symbols]
        return [], {"replacement_budget": replacement_budget,
                    "week_normal_replacements_used": week_used, "retained": [],
                    "replaced": replaced, "added": [], "exception": "portfolio_drawdown_risk_off"}

    normal_replaced = normal_candidates[:replacement_budget]
    retained.extend(item["symbol"] for item in normal_candidates[replacement_budget:])
    if market.exposure_cap <= 0:
        replaced = [{"symbol": symbol, "reason": "市场 risk_off/极端风险",
                     "kind": "market_risk", "priority": 3} for symbol in old_symbols]
        return [], {"replacement_budget": replacement_budget,
                    "week_normal_replacements_used": week_used, "retained": [],
                    "replaced": replaced, "added": [], "exception": "risk_off"}

    replaced = hard + risk_exits + normal_replaced
    initialization = len(old_symbols) == 0
    recovery = len(old_symbols) < 3 or (len(retained) < 3 and bool(hard))
    if initialization:
        desired = max(3, min(5, target_count))
    elif recovery:
        desired = 3
    else:
        desired = max(3, len(old_symbols) - len(hard))
    exited_symbols = {item["symbol"] for item in hard + risk_exits}
    ordered = [by_symbol[symbol] for symbol in retained if symbol in by_symbol]
    retained_set = set(retained)
    ordered.extend(stock for stock in stocks
                   if stock.symbol not in retained_set and stock.symbol not in exited_symbols)
    portfolio = portfolio_builder(
        snapshot, market, ordered, capital, desired, risk_per_trade, active_factors,
        allow_low_score_symbols=set(retained),
        max_adv_participation=max_adv_participation,
    )
    final_symbols = [position.symbol for position in portfolio]
    final_retained = [symbol for symbol in retained if symbol in final_symbols]
    added = [symbol for symbol in final_symbols if symbol not in old_symbols]

    constraint_evictions = [symbol for symbol in retained if symbol not in final_symbols]
    if constraint_evictions and not recovery:
        allowed = {item["symbol"] for item in normal_replaced}
        if any(symbol not in allowed for symbol in constraint_evictions):
            portfolio = portfolio_builder(
                snapshot, market, [by_symbol[symbol] for symbol in retained if symbol in by_symbol],
                capital, max(3, len(retained)), risk_per_trade, active_factors,
                allow_low_score_symbols=set(retained),
                max_adv_participation=max_adv_participation,
            )
            final_symbols = [position.symbol for position in portfolio]
            final_retained = [symbol for symbol in retained if symbol in final_symbols]
            added = []

    for position in portfolio:
        if position.symbol in final_retained:
            state = position_states[position.symbol]
            position.action = "持有"
            position.entry_state = state["entry_state"]
            position.entry_price = state["entry_price"]
            position.highest_price = round(state["highest_price"], 2)
            position.initial_stop = state["initial_stop"]
            position.protective_price = state["protective_price"]
            position.entry_at = state["entry_at"]
            position.reference_adj_factor = float(histories[position.symbol][-1].adj_factor or 1.0)

    used_now = len(normal_replaced)
    exception = "initialization" if initialization else "recovery_to_three" if recovery else None
    turnover = {
        "replacement_budget": replacement_budget,
        "week_normal_replacements_used": week_used + used_now,
        "retained": [{"symbol": symbol, "reason": "仍在当前合格前30%，使用持仓缓冲"}
                     for symbol in final_retained],
        "replaced": replaced,
        "added": [{"symbol": symbol, "reason": "当前重新评分后入选"} for symbol in added],
        "exception": exception,
    }
    return portfolio, turnover
