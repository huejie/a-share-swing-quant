from __future__ import annotations
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from math import sqrt
from statistics import mean

from .models import (Bar, DataSnapshot, ExitDecision, Lifecycle, MarketAssessment, MarketRegime,
                     PositionAdvice, StockAssessment, ThemeAssessment)

MODEL_VERSION = "swing-rules-0.3.0"
ALL_FACTORS = frozenset({"market_regime","theme_score","stock_score","risk_control"})


def _history(snapshot: DataSnapshot) -> dict[str, list[Bar]]:
    result = defaultdict(list)
    for bar in snapshot.bars: result[bar.symbol].append(bar)
    for symbol,bars in result.items():
        bars.sort(key=lambda x: x.day)
        # Tushare-style cumulative adjustment factors turn historical raw
        # prices into a latest-session comparable series.  Normalising by the
        # latest factor keeps today's executable price unchanged while removing
        # split/dividend jumps from momentum, ATR and entry-signal calculations.
        latest_factor=bars[-1].adj_factor
        if latest_factor>0 and any(bar.adj_factor!=latest_factor for bar in bars):
            adjusted=[]
            for bar in bars:
                if bar.adj_factor<=0:
                    adjusted.append(bar)
                    continue
                price_ratio=bar.adj_factor/latest_factor
                volume_ratio=latest_factor/bar.adj_factor
                adjusted.append(replace(
                    bar,
                    open=bar.open*price_ratio,
                    high=bar.high*price_ratio,
                    low=bar.low*price_ratio,
                    close=bar.close*price_ratio,
                    volume=max(0,round(bar.volume*volume_ratio)),
                    # Prices are now on the latest factor's basis.  Carry the
                    # same factor to prevent downstream helpers from applying
                    # the corporate action a second time.
                    adj_factor=latest_factor,
                ))
            result[symbol]=adjusted
    return result


def _factor(bar: Bar) -> float:
    return bar.adj_factor if bar.adj_factor > 0 else 1.0


def _adjusted_price(bar: Bar, field: str, reference_factor: float) -> float:
    """Express a historical OHLC value on the reference day's price basis."""
    return float(getattr(bar, field)) * _factor(bar) / reference_factor


def _daily_returns(bars: list[Bar], periods: int) -> list[float]:
    start=max(1,len(bars)-periods); result=[]
    for idx in range(start,len(bars)):
        previous,current=bars[idx-1],bars[idx]
        previous_adjusted=previous.close*_factor(previous)
        current_adjusted=current.close*_factor(current)
        result.append(current_adjusted/previous_adjusted-1 if previous_adjusted else 0.0)
    return result


def _return(bars: list[Bar], periods: int) -> float:
    if len(bars) < 2: return 0.0
    start_bar = bars[max(0, len(bars)-1-periods)]
    start = start_bar.close * _factor(start_bar)
    end = bars[-1].close * _factor(bars[-1])
    return end / start - 1 if start else 0


def assess_market(snapshot: DataSnapshot, active_factors: frozenset[str] | None=None) -> MarketAssessment:
    histories = _history(snapshot); latest = [v[-1] for v in histories.values()]
    returns20 = [_return(v,20) for v in histories.values()]
    returns60 = [_return(v,60) for v in histories.values()]
    trend = max(0,min(100, 50 + mean(returns60)*180))
    breadth = 100 * sum(r > 0 for r in returns20) / max(1,len(returns20))
    turnover_liquidity = max(0,min(100, 48 + mean(b.amount for b in latest)/1_000_000_000*4))
    inputs=snapshot.metadata.get("market_inputs",{}) if isinstance(snapshot.metadata,dict) else {}
    fund_flow=float(inputs.get("fund_flow_score",50.0))
    liquidity=max(0,min(100,turnover_liquidity*.65+fund_flow*.35))
    global_risk = float(inputs.get("global_risk_score",50.0))
    sentiment = max(0,min(100, 50 + mean(returns20)*240))
    valuation = float(inputs.get("valuation_score",50.0))
    growth_returns = [_return(v,60) for v in histories.values() if v[-1].board != "主板"]
    # A deliberately narrow public-source universe (for example an AKShare
    # watchlist containing only main-board names) must degrade to a neutral
    # style reading instead of crashing on an empty mean.
    style = "成长占优" if growth_returns and mean(growth_returns) > mean(returns60) else "均衡"
    components = {"trend_breadth":round((trend+breadth)/2,1), "style":62.0 if style=="成长占优" else 52.0,
                  "liquidity":round(liquidity,1), "global_risk":global_risk,
                  "sentiment":round(sentiment,1), "valuation":valuation}
    score = components["trend_breadth"]*.25 + components["style"]*.20 + liquidity*.15 + global_risk*.15 + sentiment*.15 + valuation*.10
    if score>=72: regime, cap = MarketRegime.STRONG,.90
    elif score>=60: regime, cap = MarketRegime.BULLISH_RANGE,.72
    elif score>=48: regime, cap = MarketRegime.BEARISH_RANGE,.45
    elif score>=35: regime, cap = MarketRegime.DECLINE,.18
    else: regime, cap = MarketRegime.EXTREME_RISK,0.0
    component_quality = {
        "trend_breadth": "observed_watch_universe", "style": "observed_board_proxy",
        "liquidity": str(inputs.get("fund_flow_quality", "neutral_missing")),
        "global_risk": str(inputs.get("global_risk_quality", "neutral_missing")),
        "sentiment": "observed_price_breadth_proxy",
        "valuation": str(inputs.get("valuation_quality", "neutral_missing")),
    }
    component_weights = {"trend_breadth": .25, "style": .20, "liquidity": .15,
                         "global_risk": .15, "sentiment": .15, "valuation": .10}
    completeness = sum(weight for name, weight in component_weights.items()
                       if component_quality[name] != "neutral_missing")
    coverage = len(latest) / max(1, snapshot.expected_symbols)
    confidence = max(0.0, min(1.0, completeness * min(1.0, coverage)))
    result=MarketAssessment(round(score,1),regime,cap,components,style,
                            (f"20日上涨股票占比 {breadth:.0f}%", f"60日平均动量 {mean(returns60)*100:.1f}%", f"当前{style}",
                             f"全球风险/资金代理 {global_risk:.0f}/{fund_flow:.0f}（{inputs.get('source','中性缺省')}）"),
                            round(completeness, 3), round(confidence, 3), snapshot.as_of, component_quality)
    if active_factors is not None and "market_regime" not in active_factors:
        return replace(result,score=55.0,regime=MarketRegime.BEARISH_RANGE,exposure_cap=.45,reasons=result.reasons+("消融：市场状态门控已移除",))
    return result


def assess_themes(snapshot: DataSnapshot, active_factors: frozenset[str] | None=None) -> list[ThemeAssessment]:
    histories = _history(snapshot); grouped=defaultdict(list)
    for bars in histories.values(): grouped[bars[-1].theme].append(bars)
    all_r60=mean(_return(x,60) for x in histories.values())
    results=[]
    for theme, members in grouped.items():
        r20=mean(_return(x,20) for x in members); r60=mean(_return(x,60) for x in members)
        rs=max(0,min(100,55+(r60-all_r60)*300)); breadth=100*sum(_return(x,20)>0 for x in members)/len(members)
        turnover=max(20,min(95,50+mean(x[-1].amount/mean(b.amount for b in x[-20:]) for x in members)*18))
        fundamental=mean(x[-1].quality for x in members); catalyst=mean(x[-1].catalyst for x in members)
        leadership=max(20,min(95,55+max(_return(x,60) for x in members)*150)); crowding=max(0,min(25,max(0,r20-.16)*90))
        score=rs*.30+breadth*.20+turnover*.15+fundamental*.15+catalyst*.10+leadership*.10-crowding
        if crowding > 12:
            lifecycle, lifecycle_reason = Lifecycle.CROWDED, f"20日涨幅较快且拥挤惩罚 {crowding:.1f}"
        elif r20 > .13:
            lifecycle, lifecycle_reason = Lifecycle.ACCELERATING, f"20日涨幅 {r20*100:.1f}%，进入加速观察而非追入"
        elif r20 < -.03:
            lifecycle, lifecycle_reason = Lifecycle.FADING, f"20日收益 {r20*100:.1f}%，动量转弱"
        elif score >= 72:
            lifecycle, lifecycle_reason = Lifecycle.HEALTHY, f"综合分 {score:.1f} 且未触发加速/拥挤/退潮"
        elif score >= 68:
            lifecycle, lifecycle_reason = Lifecycle.EXPANDING, f"综合分 {score:.1f}，广度与成交结构扩散"
        elif score >= 60:
            lifecycle, lifecycle_reason = Lifecycle.STARTING, f"综合分 {score:.1f}，处于启动确认区"
        else:
            lifecycle, lifecycle_reason = Lifecycle.DORMANT, f"综合分 {score:.1f}，尚未形成健康趋势"
        results.append(ThemeAssessment(theme,round(score,1),lifecycle,round(rs,1),round(breadth,1),round(turnover,1),
                                       round(fundamental,1),round(catalyst,1),round(leadership,1),round(crowding,1),
                                       lifecycle_reason,"观察池成交额相对20日均值代理（非主力净流入）"))
    results=sorted(results,key=lambda x:x.score,reverse=True)
    if active_factors is not None and "theme_score" not in active_factors:
        return [replace(x,score=65.0,lifecycle=Lifecycle.STARTING) for x in results]
    return results


def assess_stocks(snapshot: DataSnapshot, themes: list[ThemeAssessment] | None=None, capital: float=1_000_000,
                  active_factors: frozenset[str] | None=None) -> list[StockAssessment]:
    histories=_history(snapshot); resolved_themes=themes or assess_themes(snapshot,active_factors)
    theme_scores={x.name:x.score for x in resolved_themes};theme_lifecycles={x.name:x.lifecycle for x in resolved_themes}
    all_r60=mean(_return(x,60) for x in histories.values()); out=[]
    for symbol,bars in histories.items():
        last=bars[-1]; avg_amount=mean(x.amount for x in bars[-20:]); r60=_return(bars,60)
        reference_factor=_factor(last)
        rs=max(0,min(100,55+(r60-all_r60)*280))
        ma20=mean(_adjusted_price(x,"close",reference_factor) for x in bars[-20:])
        ma60=mean(_adjusted_price(x,"close",reference_factor) for x in bars[-60:])
        trend=max(0,min(100,50+(last.close/ma20-1)*220+(ma20/ma60-1)*180))
        true_ranges=[(b.high-b.low)/b.close for b in bars[-14:] if b.close]; atr=mean(true_ranges) if true_ranges else .08
        volume_structure=max(25,min(95,50+last.amount/avg_amount*18)); liquidity=max(0,min(100,45+avg_amount/100_000_000*18))
        theme_core=min(95,theme_scores.get(last.theme,50)+5); penalty=max(0,(atr-.045)*180)
        score=rs*.25+last.quality*.20+volume_structure*.15+theme_core*.15+trend*.10+liquidity*.10+last.catalyst*.05-penalty
        lifecycle=theme_lifecycles.get(last.theme,Lifecycle.DORMANT)
        allowed_lifecycle=lifecycle in {Lifecycle.STARTING,Lifecycle.EXPANDING,Lifecycle.HEALTHY}
        gates = {
            "not_st": {"passed": not last.is_st, "reason": "非ST" if not last.is_st else "ST风险"},
            "not_delisting": {"passed": not last.is_delisting, "reason": "无退市标记" if not last.is_delisting else "退市风险"},
            "regulatory": {"passed": not last.regulatory_risk, "reason": "无重大监管风险" if not last.regulatory_risk else "重大监管风险"},
            "audit": {"passed": not last.audit_abnormal, "reason": "无审计异常" if not last.audit_abnormal else "审计异常"},
            "event_window": {"passed": not last.event_risk, "reason": "无重大事件窗口" if not last.event_risk else "重大事件窗口"},
            "tradeable": {"passed": not last.suspended, "reason": "可交易" if not last.suspended else "停牌"},
            "listing_age": {"passed": last.listed_days>=120, "reason": f"上市天数 {last.listed_days}"},
            "liquidity": {"passed": avg_amount>=50_000_000, "reason": f"20日均成交额 {avg_amount:.0f} 元"},
            "capacity": {"passed": capital*.15<=avg_amount*.02, "reason": "15%最低目标仓位不超过20日均成交额2%"},
            "theme_lifecycle": {"passed": allowed_lifecycle, "reason": f"题材阶段 {lifecycle.value}"},
        }
        # Lifecycle is an entry gate, not a security-universe hard failure:
        # an existing healthy holding may be observed through acceleration,
        # while a fading theme is handled by the explicit exit state machine.
        reason=next((gate["reason"] for name,gate in gates.items()
                     if name!="theme_lifecycle" and not gate["passed"]),None)
        out.append(StockAssessment(symbol,last.name,last.theme,last.industry,round(score,1),last.close,round(atr,4),round(avg_amount,2),
                                   round(rs,1),round(trend,1),(f"题材强度 {theme_scores.get(last.theme,0):.1f}",f"60日相对强度 {rs:.1f}",f"中期趋势质量 {trend:.1f}"),
                                   reason is None,reason,lifecycle.value,gates))
    if active_factors is not None and "stock_score" not in active_factors:
        out=[replace(x,score=65.0,reasons=("消融：个股横截面评分已移除",)) for x in out]
    return sorted(out,key=lambda x:x.score,reverse=True)


def trailing_stop(entry: float, peak: float, activate_gain: float=.15, giveback: float=.30) -> float | None:
    # Compare prices rather than a derived return so the documented boundary
    # (for example 100 -> 115 at 15%) is not lost to binary float rounding.
    if entry<=0 or peak + 1e-9 < entry * (1 + activate_gain): return None
    return round(entry+(1-giveback)*(peak-entry),2)


def update_trailing_stop(previous_stop: float | None, entry: float, peak: float,
                         corporate_action_price_ratio: float=1.0) -> float | None:
    """Keep protection monotonic while allowing an explicit split/dividend price adjustment.

    ``corporate_action_price_ratio`` is new-price / old-price.  For example a
    two-for-one split uses 0.5; callers must source it from verified company
    action data rather than infer it from a price jump.
    """
    if corporate_action_price_ratio<=0: raise ValueError("corporate action ratio must be positive")
    candidate=trailing_stop(entry,peak)
    adjusted_previous=round(previous_stop*corporate_action_price_ratio,2) if previous_stop is not None else None
    if candidate is None:return adjusted_previous
    if adjusted_previous is None:return candidate
    return max(adjusted_previous,candidate)


def entry_signal(bars: list[Bar]) -> tuple[str, tuple[float,float]] | None:
    """Return a confirmed close signal, otherwise ``None``.

    A non-breakout is not automatically a pullback.  Both patterns require an
    established rising medium-term trend and an explicit participation or
    re-strengthening confirmation.  Execution remains next-session only.
    """
    if len(bars)<61:return None
    last=bars[-1]; prior=bars[-61:-1]; reference_factor=_factor(last)
    platform=max((_adjusted_price(x,"high",reference_factor) for x in prior),default=last.high)
    # Put historical volume on the latest share-count basis so a split does
    # not masquerade as breakout participation.
    avg_volume=(mean(x.volume*reference_factor/_factor(x) for x in bars[-21:-1])
                if len(bars)>1 else last.volume)
    closes=[_adjusted_price(x,"close",reference_factor) for x in bars]
    ma20=mean(closes[-20:]);ma60=mean(closes[-60:]);previous_ma20=mean(closes[-25:-5])
    rising_trend=last.close>ma20>ma60 and ma20>previous_ma20
    if rising_trend and last.close>=platform*.995 and last.volume>=avg_volume*1.05:
        return "平台突破",(round(platform*.995,2),round(platform*1.015,2))
    previous=bars[-2]
    pullback_zone=ma20*.97<=last.close<=ma20*1.05
    contracted=last.volume<=avg_volume*1.10
    restrengthened=last.close>previous.close or last.close>=last.open
    if rising_trend and pullback_zone and contracted and restrengthened:
        return "趋势回踩",(round(ma20*.985,2),round(ma20*1.02,2))
    return None


def evaluate_exit(*, entry:float, peak:float, close:float, initial_stop:float, holding_days:int,
                  hard_risk:bool=False, portfolio_drawdown:float=0, extreme_market:bool=False,
                  theme_fading:bool=False, trend_broken:bool=False, previous_protective:float|None=None,
                  corporate_action_price_ratio:float=1.0, max_portfolio_drawdown:float=.18) -> ExitDecision:
    protective=update_trailing_stop(previous_protective,entry,peak,corporate_action_price_ratio)
    checks=((1,hard_risk,"财务、退市、处罚等硬风险"),(2,portfolio_drawdown<=-abs(max_portfolio_drawdown),f"组合达到{abs(max_portfolio_drawdown)*100:.0f}%硬风控"),
            (3,extreme_market,"市场进入极端风险"),(4,theme_fading,"题材进入退潮"),(5,trend_broken,"个股趋势失效"),
            (6,close<=initial_stop,"触发ATR初始止损"),(6,protective is not None and close<=protective,"最高浮盈回吐30%"),
            (7,holding_days>=80,"达到80个交易日时间退出"))
    for priority,trigger,reason in checks:
        if trigger:return ExitDecision(True,priority,reason,protective)
    return ExitDecision(False,None,"继续持有；退出条件均未触发",protective)


def build_portfolio(snapshot: DataSnapshot, market: MarketAssessment, stocks: list[StockAssessment], capital: float=1_000_000,
                    target_count: int=4, risk_per_trade: float=.0125, active_factors: frozenset[str] | None=None,
                    allow_low_score_symbols: set[str] | None=None) -> list[PositionAdvice]:
    if market.exposure_cap<=0: return []
    max_count_by_exposure=int((market.exposure_cap+1e-9)/.15)
    desired_count=min(max(3,min(5,target_count)),max_count_by_exposure)
    if desired_count<3:return []
    selected=[]; theme_count=defaultdict(int); industries=set(); histories=_history(snapshot);allow_low_score_symbols=allow_low_score_symbols or set()
    for stock in stocks:
        if not stock.eligible or (stock.score<58 and stock.symbol not in allow_low_score_symbols) or theme_count[stock.theme]>=2: continue
        lifecycle_gate=stock.gate_results.get("theme_lifecycle",{})
        if stock.symbol not in allow_low_score_symbols and not lifecycle_gate.get("passed",False):continue
        if stock.symbol not in allow_low_score_symbols and entry_signal(histories[stock.symbol]) is None:continue
        # Duplicate industries are allowed up to the explicit two-name/theme
        # limit; the aggregate industrial-chain cap is applied after sizing.
        # This avoids an order-dependent first-pass skip that could miss a
        # valid 3-name portfolio even when a later name supplies diversity.
        candidate_returns=_daily_returns(histories[stock.symbol],60)
        too_correlated=False
        for chosen in selected:
            chosen_bars=histories[chosen.symbol]
            chosen_returns=_daily_returns(chosen_bars,60)
            if correlation(candidate_returns,chosen_returns)>.95:
                too_correlated=True;break
        if too_correlated:continue
        selected.append(stock); theme_count[stock.theme]+=1; industries.add(stock.industry)
        if len(selected)>=desired_count: break
    if len(selected)<3: return []  # cash/no-trade is safer than forced concentration
    raw=[]
    for s in selected:
        stop_distance=max(.07,min(.12,s.atr_pct*2.5))
        # Risk budget is applied to the 60% initial tranche.  The confirmed
        # target remains inside the documented 15%-25% band.
        raw.append(min(.25,max(.15,risk_per_trade/(stop_distance*.60)),s.avg_amount_20d*.02/capital))
    if len(selected)==3: cap=min(market.exposure_cap,.75)
    else: cap=market.exposure_cap
    if active_factors is not None and "risk_control" not in active_factors:
        weights=[round(min(.25,cap/len(selected)),4) for _ in selected]
    else:
        if sum(raw)<=cap:
            weights=[round(x,4) for x in raw]
        else:
            base=.15*len(raw);extra=max(0.0,cap-base);raw_extra=[max(0.0,x-.15) for x in raw]
            extra_total=sum(raw_extra)
            weights=[round(.15+(extra*(item/extra_total) if extra_total else 0.0),4) for item in raw_extra]
    # Industry is the MVP's conservative risk-cluster/industrial-chain proxy.
    # A pair of correlated names may each pass the 25% single-name cap but must
    # not exceed the documented 45% aggregate chain exposure.
    by_industry=defaultdict(list)
    for idx,stock in enumerate(selected): by_industry[stock.industry].append(idx)
    for indices in by_industry.values():
        aggregate=sum(weights[i] for i in indices)
        if aggregate>.45:
            factor=.45/aggregate
            for i in indices: weights[i]=round(weights[i]*factor,4)
    advice=[]
    for s,w in zip(selected,weights):
        bars=histories[s.symbol]; entry=s.close; reference_factor=_factor(bars[-1])
        peak=entry; stop=round(entry*(1-max(.07,min(.12,s.atr_pct*2.5))),2)
        signal=entry_signal(bars)
        # Retained holdings need no fresh entry signal; their original entry
        # state is restored by the service's position-state reconciliation.
        state,zone=signal if signal is not None else ("持仓复核",(entry,entry))
        next_review=snapshot.as_of+timedelta(days=(7-snapshot.as_of.weekday()) or 7)
        advice.append(PositionAdvice(s.symbol,s.name,s.theme,"待买",state,zone,w,round(w*.60,4),0.0,entry,stop,None,round(peak,2),s.score,
                                     s.reasons,"收盘跌破初始止损、题材退潮或出现硬风险",("模型组合，不代表真实持仓","次日成交且受涨跌停/流动性约束"),(40,80),next_review,MODEL_VERSION,snapshot.as_of,
                                     snapshot.as_of))
    return advice


def correlation(a: list[float], b: list[float]) -> float:
    n=min(len(a),len(b)); a,b=a[-n:],b[-n:]
    if n<2:return 0
    ma,mb=mean(a),mean(b); da=[x-ma for x in a]; db=[x-mb for x in b]
    denom=sqrt(sum(x*x for x in da)*sum(x*x for x in db))
    return sum(x*y for x,y in zip(da,db))/denom if denom else 0
