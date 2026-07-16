from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from math import sqrt
from statistics import mean
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .engine import MODEL_VERSION, assess_market, assess_stocks, assess_themes, build_portfolio, evaluate_exit
from .models import Bar, DataSnapshot, jsonable
from .portfolio_policy import buffered_portfolio, weekly_theme_names


@dataclass(frozen=True)
class Fill:
    signal_day: date
    fill_day: date
    symbol: str
    side: str
    price: float
    shares: int
    fee: float
    status: str = "filled"
    reason: str = "rebalance"


@dataclass
class BacktestResult:
    initial_capital: float
    final_equity: float
    total_return: float
    annualized_return: float
    max_drawdown: float
    volatility: float
    sharpe: float
    fills: list[Fill]
    equity_curve: list[dict]
    assumptions: dict
    order_ledger: list[dict] = field(default_factory=list)
    policy_ledger: list[dict] = field(default_factory=list)


def _cost(value: float, side: str, commission: float = .0003, stamp: float = .0005,
          multiplier: float = 1.0) -> float:
    if multiplier <= 0:
        raise ValueError("transaction_cost_multiplier must be positive")
    return (max(5.0, value * commission) + (value * stamp if side == "sell" else 0)) * multiplier


def _neutral_market_inputs() -> dict[str, Any]:
    return {
        "global_risk_score": 50.0,
        "global_risk_quality": "neutral_missing",
        "fund_flow_score": 50.0,
        "fund_flow_quality": "neutral_missing",
        "valuation_score": 50.0,
        "valuation_quality": "neutral_missing",
        "source": "回测时点市场输入缺失，显式中性降级",
    }


def _history_day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _metadata_as_of(metadata: Mapping[str, Any], day: date) -> tuple[dict[str, Any], str]:
    """Return metadata containing only market inputs visible by ``day``.

    A snapshot-level ``market_inputs`` value normally describes the final
    observation and must never be copied backwards through a historical
    backtest.  Providers may supply ``market_inputs_history`` either as a
    date-keyed mapping or as records carrying ``date``/``as_of``.  Without a
    usable history the market component is deliberately neutral and degraded.
    """
    copied = dict(metadata)
    history = metadata.get("market_inputs_history")
    candidates: list[tuple[date, Mapping[str, Any]]] = []
    if isinstance(history, Mapping):
        for raw_day, raw_value in history.items():
            history_day = _history_day(raw_day)
            if history_day is not None and isinstance(raw_value, Mapping):
                candidates.append((history_day, raw_value))
    elif isinstance(history, list):
        for raw_value in history:
            if not isinstance(raw_value, Mapping):
                continue
            history_day = _history_day(raw_value.get("as_of") or raw_value.get("date"))
            if history_day is not None:
                candidates.append((history_day, raw_value))
    visible = [(history_day, value) for history_day, value in candidates if history_day <= day]
    if visible:
        selected_day, selected = max(visible, key=lambda item: item[0])
        nested = selected.get("market_inputs") if isinstance(selected, Mapping) else None
        values = dict(nested if isinstance(nested, Mapping) else selected)
        for field_name in ("date", "as_of", "market_inputs"):
            values.pop(field_name, None)
        copied["market_inputs"] = values
        quality = "point_in_time_history"
        copied["backtest_market_inputs"] = {
            "status": quality, "selected_as_of": selected_day.isoformat(),
            "requested_as_of": day.isoformat(),
        }
    else:
        copied["market_inputs"] = _neutral_market_inputs()
        quality = "neutral_missing"
        copied["backtest_market_inputs"] = {
            "status": quality, "selected_as_of": None,
            "requested_as_of": day.isoformat(),
            "warning": "最终快照 market_inputs 已隔离，缺少历史时点输入",
        }
    return copied, quality


def _ledger_event(ledger: list[dict], *, day: date, order: Mapping[str, Any], status: str,
                  reason: str, filled: int = 0, price: float | None = None,
                  capacity: int | None = None) -> None:
    ledger.append({
        "day": day.isoformat(), "signal_day": order["signal_day"].isoformat(),
        "symbol": order["symbol"], "side": order["side"], "stage": order["stage"],
        "status": status, "reason": reason,
        "requested_shares": int(order["requested_shares"]),
        "filled_shares": int(filled), "remaining_shares": int(order["remaining_shares"] - filled),
        "capacity_shares": capacity, "price": round(price, 4) if price is not None else None,
    })


def apply_rebalance_policy(snapshot, market, stocks, *, previous_decision: dict | None,
                           capital: float, target_count: int, risk_per_trade: float,
                           max_drawdown: float, max_adv_participation: float = .02,
                           portfolio_drawdown: float = 0.0,
                           active_factors: frozenset[str] | None = None):
    """Backtest adapter for the exact production model-portfolio policy."""
    return buffered_portfolio(
        snapshot, market, stocks, previous_decision=previous_decision,
        capital=capital, target_count=target_count, risk_per_trade=risk_per_trade,
        max_adv_participation=max_adv_participation,
        max_drawdown=max_drawdown, portfolio_drawdown=portfolio_drawdown,
        active_factors=active_factors, portfolio_builder=build_portfolio,
    )


def run_backtest(snapshot: DataSnapshot, capital: float = 1_000_000, rebalance_days: int = 10,
                 slippage_bps: float = 8.0, active_factors: frozenset[str] | None = None,
                 max_portfolio_drawdown: float = .18,
                 target_count: int = 4, risk_per_trade: float = .0125,
                 max_adv_participation: float = .02,
                 previous_model_decision: dict | None = None,
                 transaction_cost_multiplier: float = 1.0) -> BacktestResult:
    """Close signals, next-session execution and persistent capacity-limited orders."""
    days = sorted({bar.day for bar in snapshot.bars})
    if not days:
        raise ValueError("backtest requires at least one market bar")
    by_day = {day: {} for day in days}
    symbol_last_day: dict[str, date] = {}
    for bar in snapshot.bars:
        by_day[bar.day][bar.symbol] = bar
        symbol_last_day[bar.symbol] = max(symbol_last_day.get(bar.symbol, bar.day), bar.day)

    cash = capital
    holdings: dict[str, int] = {}
    fills: list[Fill] = []
    order_ledger: list[dict] = []
    policy_ledger: list[dict] = []
    curve: list[dict] = []
    pending_orders: dict[str, dict] = {}
    position_state: dict[str, dict] = {}
    portfolio_peak = capital
    last_closes: dict[str, float] = {}
    last_observed: dict[str, Bar] = {}
    market_input_qualities: list[str] = []
    corporate_action_events: list[dict] = []
    forced_risk_disposals: list[dict] = []
    warmup = 60

    explicit_delisted=set(snapshot.metadata.get("delisted_symbols",[]) if isinstance(snapshot.metadata,dict) else [])

    def terminal_reason(symbol: str, day: date) -> str | None:
        previous=last_observed.get(symbol)
        if symbol in explicit_delisted:return "provider_confirmed_delisted"
        if previous is not None and previous.day<day and previous.is_delisting:
            return "delisting_flag_then_permanent_quote_absence"
        if (previous is not None and previous.day<day-timedelta(days=20)
                and not previous.suspended):
            return "quote_absent_over_20_calendar_days_without_suspension"
        if previous is not None and previous.suspended and previous.day<day-timedelta(days=60):
            return "suspension_quote_stale_over_60_calendar_days"
        return None

    def portfolio_value(day_bars: Mapping[str, Bar], field_name: str) -> float:
        value = cash
        for symbol, quantity in holdings.items():
            bar = day_bars.get(symbol)
            price = float(getattr(bar, field_name)) if bar is not None else last_closes.get(symbol, 0.0)
            value += quantity * price
        return value

    def create_order(symbol: str, side: str, quantity: int, signal_day: date, stage: str,
                     reason: str, initial_stop: float | None = None) -> None:
        quantity = int(quantity // 100) * 100
        order = {
            "symbol": symbol, "side": side, "requested_shares": quantity,
            "remaining_shares": quantity, "signal_day": signal_day,
            "stage": stage, "reason": reason, "initial_stop": initial_stop,
        }
        if quantity <= 0:
            _ledger_event(order_ledger, day=signal_day, order=order, status="not_created",
                          reason="below_board_lot")
            return
        pending_orders[symbol] = order
        _ledger_event(order_ledger, day=signal_day, order=order, status="created", reason=reason)

    for index, day in enumerate(days):
        bars = by_day[day]

        # Account-level corporate actions happen before the opening auction.
        # adj_factor is signal-only and never changes shares or cash.
        for symbol,bar in sorted(bars.items()):
            if symbol not in holdings and symbol not in pending_orders:continue
            multiplier=float(bar.share_multiplier or 1.0)
            dividend=float(bar.cash_dividend_per_share or 0.0)
            if abs(multiplier-1.0)<1e-12 and abs(dividend)<1e-12:continue
            if multiplier<=0:raise ValueError(f"invalid share multiplier for {symbol}")
            old_quantity=holdings.get(symbol,0);new_quantity=int(round(old_quantity*multiplier))
            cash_delta=old_quantity*dividend;cash+=cash_delta
            if old_quantity:
                holdings[symbol]=new_quantity
                state=position_state.get(symbol)
                if state is not None:
                    for field_name in ("entry","peak","initial_stop","protective"):
                        if state.get(field_name) is not None:
                            state[field_name]=float(state[field_name])/multiplier
            order=pending_orders.get(symbol)
            if order is not None:
                order["requested_shares"]=int(round(order["requested_shares"]*multiplier))
                order["remaining_shares"]=int(round(order["remaining_shares"]*multiplier))
                if order.get("initial_stop") is not None:
                    order["initial_stop"]=float(order["initial_stop"])/multiplier
            event={"day":day.isoformat(),"symbol":symbol,"status":"applied",
                   "event":"corporate_action","share_multiplier":multiplier,
                   "cash_dividend_per_share":dividend,"old_shares":old_quantity,
                   "new_shares":new_quantity,"cash_delta":round(cash_delta,2),
                   "adj_factor_not_used_for_accounting":True}
            corporate_action_events.append(event);order_ledger.append(event)

        # A proved delisting/permanent quote loss is a risk write-down, never
        # an imaginary sell fill and never an unlimited last-close valuation.
        day_disposals=[]
        for symbol,quantity in list(holdings.items()):
            if symbol in bars:continue
            reason=terminal_reason(symbol,day)
            if reason is None:continue
            state=position_state.get(symbol,{})
            event={"day":day.isoformat(),"symbol":symbol,"side":"write_down",
                   "stage":"forced_risk_disposal","status":"written_down","reason":reason,
                   "shares":quantity,"book_value_written_down":round(quantity*float(state.get("entry",0)),2),
                   "recovery_price":0.0}
            day_disposals.append(event);forced_risk_disposals.append(event);order_ledger.append(event)
            pending_orders.pop(symbol,None);holdings.pop(symbol,None);position_state.pop(symbol,None)

        # Sells release cash before buys. Every order retains its unfilled
        # remainder across sessions; absence of a bar never creates proceeds.
        ordered = sorted(list(pending_orders.items()), key=lambda item: item[1]["side"] != "sell")
        for symbol, order in ordered:
            if pending_orders.get(symbol) is not order:
                continue
            bar = bars.get(symbol)
            if bar is None:
                reason=terminal_reason(symbol,day)
                _ledger_event(order_ledger, day=day, order=order,
                              status="cancelled" if reason else "blocked",
                              reason=reason or "temporary_missing_bar")
                if reason:pending_orders.pop(symbol,None)
                continue
            side = order["side"]
            if bar.suspended or (side == "buy" and bar.limit_up) or (side == "sell" and bar.limit_down):
                _ledger_event(order_ledger, day=day, order=order, status="blocked",
                              reason="suspended_or_price_limit")
                continue
            if side == "buy" and bar.is_delisting:
                _ledger_event(order_ledger, day=day, order=order, status="cancelled",
                              reason="delisting_risk")
                pending_orders.pop(symbol, None)
                continue

            capacity = int((max(0, bar.volume) * .01) // 100) * 100
            remaining = int(order["remaining_shares"])
            price = bar.open * (1 + (slippage_bps / 10000 if side == "buy" else -slippage_bps / 10000))
            if side == "buy":
                affordable = int((max(0.0, cash - 5.0) / (price * 1.0003)) // 100) * 100
                quantity = min(remaining, capacity, affordable)
                while quantity > 0 and quantity * price + _cost(
                    quantity * price, "buy", multiplier=transaction_cost_multiplier,
                ) > cash:
                    quantity -= 100
            else:
                quantity = min(remaining, capacity, holdings.get(symbol, 0))
            if quantity <= 0:
                reason = "zero_daily_capacity" if capacity <= 0 else "cash_or_position_unavailable"
                _ledger_event(order_ledger, day=day, order=order, status="blocked",
                              reason=reason, capacity=capacity, price=price)
                continue

            value = quantity * price
            fee = _cost(value, side, multiplier=transaction_cost_multiplier)
            order["remaining_shares"] = remaining - quantity
            status = "filled" if order["remaining_shares"] == 0 else "partial"
            if side == "buy":
                old_quantity = holdings.get(symbol, 0)
                old_entry = float(position_state.get(symbol, {}).get("entry", price))
                cash -= value + fee
                holdings[symbol] = old_quantity + quantity
                average_entry = (old_quantity * old_entry + quantity * price) / (old_quantity + quantity)
                previous = position_state.get(symbol, {})
                position_state[symbol] = {
                    "entry": average_entry, "peak": max(float(previous.get("peak", price)), bar.high),
                    "initial_stop": float(previous.get("initial_stop") or order.get("initial_stop") or price * .90),
                    "entry_index": int(previous.get("entry_index", index)),
                    "protective": previous.get("protective"),
                }
            else:
                cash += value - fee
                holdings[symbol] = holdings.get(symbol, 0) - quantity
                if holdings[symbol] <= 0:
                    holdings.pop(symbol, None)
                    position_state.pop(symbol, None)
            fills.append(Fill(order["signal_day"], day, symbol, side, round(price, 2), quantity,
                              round(fee, 2), status=status, reason=order["reason"]))
            _ledger_event(order_ledger, day=day, order={**order, "remaining_shares": remaining},
                          status=status, reason=order["reason"], filled=quantity,
                          capacity=capacity, price=price)
            if order["remaining_shares"] == 0:
                pending_orders.pop(symbol, None)

        for symbol, bar in bars.items():
            last_closes[symbol] = bar.close
            last_observed[symbol] = bar
        close_equity = portfolio_value(bars, "close")
        market_value=close_equity-cash
        assert abs((cash+market_value)-close_equity)<.01
        portfolio_peak = max(portfolio_peak, close_equity)
        unpriced = [symbol for symbol in holdings if symbol not in bars]
        curve.append({
            "date": day.isoformat(), "equity": round(close_equity, 2), "cash": round(cash, 2),
            "market_value":round(market_value,2),
            "positions": len(holdings), "pending_orders": len(pending_orders),
            "valuation_degraded": bool(unpriced), "unpriced_symbols": unpriced,
            "valuation_fallback":"last_observed_close" if unpriced else None,
            "forced_risk_disposals":day_disposals,
        })

        if index >= warmup and (holdings or (index - warmup) % rebalance_days == 0):
            subbars = [bar for bar in snapshot.bars if bar.day <= day]
            sub_metadata, input_quality = _metadata_as_of(snapshot.metadata, day)
            market_input_qualities.append(input_quality)
            sub_as_of = datetime.combine(day, time(15, 0), ZoneInfo("Asia/Shanghai"))
            sub = DataSnapshot(sub_as_of, subbars, snapshot.provider, snapshot.expected_symbols, sub_metadata)
            market = assess_market(sub, active_factors)
            themes = assess_themes(sub, active_factors)
            selected_theme_names = weekly_theme_names(sub, themes, previous_model_decision)
            stocks = assess_stocks(
                sub, themes, close_equity, active_factors,
                selected_themes=set(selected_theme_names),
                max_adv_participation=max_adv_participation,
            )
            theme_by_name = {item.name: item for item in themes}
            stock_by_symbol = {item.symbol: item for item in stocks}
            portfolio_drawdown = close_equity / portfolio_peak - 1 if portfolio_peak else 0.0

            for symbol in list(holdings):
                if symbol in pending_orders and pending_orders[symbol]["side"] == "sell":
                    continue
                bar = bars.get(symbol)
                state = position_state.get(symbol)
                if bar is None:
                    continue
                if state is None:
                    continue
                state["peak"] = max(float(state["peak"]), bar.high)
                stock = stock_by_symbol.get(symbol)
                theme = theme_by_name.get(stock.theme) if stock else None
                entry_day = days[int(state["entry_index"])]
                peer_returns = []
                peer_histories = {}
                for peer_bar in subbars:
                    if peer_bar.day >= entry_day:
                        peer_histories.setdefault(peer_bar.symbol, []).append(peer_bar)
                for peer_bars in peer_histories.values():
                    if len(peer_bars) < 2:
                        continue
                    start_bar, end_bar = peer_bars[0], peer_bars[-1]
                    adjusted_start = (start_bar.close * start_bar.adj_factor / end_bar.adj_factor
                                      if end_bar.adj_factor > 0 else start_bar.close)
                    if adjusted_start > 0:
                        peer_returns.append(end_bar.close / adjusted_start - 1)
                market_return = mean(peer_returns) if peer_returns else None
                position_return = bar.close / float(state["entry"]) - 1 if state["entry"] else None
                excess_return = (position_return - market_return
                                 if position_return is not None and market_return is not None else None)
                decision = evaluate_exit(
                    entry=float(state["entry"]), peak=float(state["peak"]), close=bar.close,
                    initial_stop=float(state["initial_stop"]),
                    holding_days=index - int(state["entry_index"]) + 1,
                    hard_risk=bar.is_delisting or bool(stock and not stock.eligible),
                    portfolio_drawdown=portfolio_drawdown,
                    extreme_market=getattr(market, "exposure_cap", 1.0) <= 0,
                    theme_fading=getattr(theme, "lifecycle", None) == "退潮",
                    trend_broken=bool(stock and stock.trend < 35),
                    previous_protective=state.get("protective"),
                    max_portfolio_drawdown=max_portfolio_drawdown,
                    excess_return=excess_return,
                )
                state["protective"] = decision.protective_price
                if decision.should_exit:
                    if symbol in pending_orders and pending_orders[symbol]["side"] == "buy":
                        cancelled = pending_orders.pop(symbol)
                        _ledger_event(order_ledger, day=day, order=cancelled, status="cancelled",
                                      reason="risk_exit_superseded_buy")
                    create_order(symbol, "sell", holdings[symbol], day, "risk_exit", decision.reason)

            if (index - warmup) % rebalance_days == 0:
                portfolio, turnover = apply_rebalance_policy(
                    sub, market, stocks, previous_decision=previous_model_decision,
                    capital=close_equity, target_count=target_count, risk_per_trade=risk_per_trade,
                    max_adv_participation=max_adv_participation,
                    max_drawdown=max_portfolio_drawdown,
                    portfolio_drawdown=portfolio_drawdown, active_factors=active_factors,
                )
                policy_ledger.append({
                    "date": day.isoformat(),
                    "target_symbols": [item.symbol for item in portfolio],
                    "selected_theme_names": list(selected_theme_names),
                    "turnover": turnover,
                })
                previous_model_decision = {
                    "model_version": getattr(portfolio[0], "model_version", None) if portfolio else None,
                    "data_timestamp": sub.as_of.isoformat(),
                    "snapshot": {
                        "portfolio": [jsonable(item) if hasattr(item,"__dataclass_fields__")
                                      else dict(vars(item)) for item in portfolio],
                        "selected_theme_names": list(selected_theme_names),
                    },
                    "turnover": turnover,
                }
                # Empty/risk-off portfolios still belong to the current model
                # lineage and must preserve weekly replacement/theme state.
                if previous_model_decision["model_version"] is None:
                    previous_model_decision["model_version"] = MODEL_VERSION
                desired = {item.symbol: item for item in portfolio}
                for symbol, order in list(pending_orders.items()):
                    if order["side"] == "buy" and symbol not in desired:
                        pending_orders.pop(symbol)
                        _ledger_event(order_ledger, day=day, order=order, status="cancelled",
                                      reason="signal_no_longer_confirmed")
                for symbol, quantity in list(holdings.items()):
                    if symbol not in desired and symbol not in pending_orders:
                        create_order(symbol, "sell", quantity, day, "rebalance_exit", "scheduled_rebalance")
                for symbol, advice in desired.items():
                    if symbol in pending_orders:
                        continue
                    close = bars.get(symbol).close if symbol in bars else last_closes.get(symbol, 0.0)
                    if close <= 0:
                        continue
                    target_weight = float(advice.target_weight)
                    if symbol not in holdings:
                        weight = float(getattr(advice, "initial_weight", target_weight * .60))
                        target_shares = int((close_equity * weight / close) // 100) * 100
                        create_order(symbol, "buy", target_shares, day, "initial",
                                     "initial_entry_50_70pct", getattr(advice, "initial_stop", None))
                    else:
                        target_shares = int((close_equity * target_weight / close) // 100) * 100
                        delta_shares=target_shares-holdings[symbol]
                        if delta_shares>0:
                            create_order(symbol, "buy", delta_shares, day, "add_confirmation",
                                         "subsequent_confirmation_to_target", getattr(advice, "initial_stop", None))
                        elif delta_shares<0:
                            create_order(symbol,"sell",-delta_shares,day,"target_reduction",
                                         "model_target_weight_decreased")

    equities = [item["equity"] for item in curve]
    daily = [equities[index] / equities[index - 1] - 1 for index in range(1, len(equities)) if equities[index - 1]]
    peak = equities[0]
    maximum_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        maximum_drawdown = min(maximum_drawdown, equity / peak - 1)
    total = equities[-1] / capital - 1
    years = max((days[-1] - days[0]).days / 365.25, 1 / 252)
    annualized = (equities[-1] / capital) ** (1 / years) - 1
    volatility = ((sum((value - mean(daily)) ** 2 for value in daily) / max(1, len(daily) - 1)) ** .5
                  * sqrt(252) if daily else 0)
    sharpe = mean(daily) * 252 / volatility if volatility else 0
    degraded_days = sum(quality != "point_in_time_history" for quality in market_input_qualities)
    assumptions = {
        "execution": "next trading-day open", "commission": .0003, "minimum_commission": 5,
        "stamp_tax_sell": .0005, "slippage_bps": slippage_bps, "lot_size": 100,
        "transaction_cost_multiplier": transaction_cost_multiplier,
        "max_daily_amount_participation": .01, "max_daily_volume_participation": .01,
        "suspended_limit_constraints": True, "persistent_partial_orders": True,
        "two_stage_entry": "initial_weight_then_later_target_confirmation",
        "market_inputs_policy": "point_in_time_history_only_else_neutral",
        "market_inputs_degraded": degraded_days > 0,
        "market_inputs_degraded_evaluations": degraded_days,
        "production_holding_buffer_shared": True,
        "shared_policy": "portfolio_policy.buffered_portfolio",
        "max_portfolio_drawdown": max_portfolio_drawdown, "automatic_trading": False,
        "max_adv_participation": max_adv_participation,
        "pending_orders_at_end": len(pending_orders),
        "corporate_action_accounting":"share_multiplier_and_cash_dividend_event_pulses",
        "corporate_action_events":len(corporate_action_events),
        "forced_risk_write_downs":len(forced_risk_disposals),
        "missing_price_policy":"temporary_last_close_else_explicit_terminal_write_down",
    }
    return BacktestResult(
        capital, round(equities[-1], 2), round(total, 4), round(annualized, 4),
        round(maximum_drawdown, 4), round(volatility, 4), round(sharpe, 2),
        fills, curve, assumptions, order_ledger, policy_ledger,
    )


def result_dict(result: BacktestResult) -> dict:
    data = asdict(result)
    for fill in data["fills"]:
        fill["signal_day"] = fill["signal_day"].isoformat()
        fill["fill_day"] = fill["fill_day"].isoformat()
    return data
