from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date
from math import sqrt
from statistics import mean
from .models import Bar, DataSnapshot
from .engine import assess_market, assess_themes, assess_stocks, build_portfolio, evaluate_exit


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


def _cost(value: float, side: str, commission=.0003, stamp=.0005) -> float:
    return max(5.0, value*commission) + (value*stamp if side=="sell" else 0)


def run_backtest(snapshot: DataSnapshot, capital: float=1_000_000, rebalance_days: int=10,
                 slippage_bps: float=8.0, active_factors: frozenset[str] | None=None,
                 max_portfolio_drawdown: float=.18) -> BacktestResult:
    """Daily event loop: close signal, next trading-day open execution, no same-day hindsight."""
    days=sorted({b.day for b in snapshot.bars}); by_day={d:{} for d in days}
    for b in snapshot.bars: by_day[b.day][b.symbol]=b
    cash=capital; holdings:dict[str,int]={}; fills=[]; curve=[]; pending:dict[str,dict]|None=None; signal_day=None
    position_state:dict[str,dict]={};pending_exits:dict[str,dict]={};portfolio_peak=capital
    warmup=60
    for i,day in enumerate(days):
        bars=by_day[day]
        # Risk exits are close signals and therefore execute no earlier than
        # the next tradable session.  Blocked exits remain pending.
        for sym,request in list(pending_exits.items()):
            if sym not in holdings:
                pending_exits.pop(sym,None);continue
            b=bars.get(sym)
            if b is None or b.suspended or b.limit_down:continue
            px=b.open*(1-slippage_bps/10000);qty=holdings.pop(sym);value=px*qty;fee=_cost(value,"sell");cash+=value-fee
            fills.append(Fill(request["signal_day"],day,sym,"sell",round(px,2),qty,round(fee,2),reason=request["reason"]))
            position_state.pop(sym,None);pending_exits.pop(sym,None)
        if pending is not None:
            # liquidate names no longer desired; blocked on suspended/limit-down
            for sym in list(holdings):
                if sym not in pending and sym in bars and not bars[sym].suspended and not bars[sym].limit_down:
                    b=bars[sym]; px=b.open*(1-slippage_bps/10000); qty=holdings.pop(sym); value=px*qty; fee=_cost(value,"sell"); cash+=value-fee
                    fills.append(Fill(signal_day,day,sym,"sell",round(px,2),qty,round(fee,2),reason="scheduled_rebalance"))
                    position_state.pop(sym,None)
            equity=cash+sum(qty*bars[s].open for s,qty in holdings.items() if s in bars)
            for sym,order in pending.items():
                if sym in holdings or sym not in bars: continue
                b=bars[sym]
                if b.suspended or b.limit_up: continue
                weight=float(order["weight"])
                px=b.open*(1+slippage_bps/10000); max_value=min(equity*weight,b.amount*.01); qty=int(max_value/px/100)*100
                if qty<=0: continue
                value=qty*px; fee=_cost(value,"buy")
                if value+fee<=cash:
                    cash-=value+fee;holdings[sym]=qty
                    fills.append(Fill(signal_day,day,sym,"buy",round(px,2),qty,round(fee,2),reason="confirmed_entry"))
                    position_state[sym]={"entry":px,"peak":max(px,b.high),
                        "initial_stop":float(order.get("initial_stop") or px*.90),
                        "entry_index":i,"protective":None}
            pending=None
        close_equity=cash+sum(qty*bars[s].close for s,qty in holdings.items() if s in bars)
        portfolio_peak=max(portfolio_peak,close_equity)
        curve.append({"date":day.isoformat(),"equity":round(close_equity,2),"cash":round(cash,2),"positions":len(holdings)})
        if i>=warmup and (holdings or (i-warmup)%rebalance_days==0):
            subbars=[b for b in snapshot.bars if b.day<=day]
            sub=DataSnapshot(snapshot.as_of,subbars,snapshot.provider,snapshot.expected_symbols,snapshot.metadata)
            m=assess_market(sub,active_factors); t=assess_themes(sub,active_factors); s=assess_stocks(sub,t,close_equity,active_factors); p=build_portfolio(sub,m,s,close_equity,active_factors=active_factors)
            theme_by_name={item.name:item for item in t};stock_by_symbol={item.symbol:item for item in s}
            portfolio_drawdown=close_equity/portfolio_peak-1 if portfolio_peak else 0.0
            for sym in list(holdings):
                b=bars.get(sym);state=position_state.get(sym)
                if b is None or state is None or sym in pending_exits:continue
                state["peak"]=max(float(state["peak"]),b.high)
                stock=stock_by_symbol.get(sym);theme=theme_by_name.get(stock.theme) if stock else None
                decision=evaluate_exit(
                    entry=float(state["entry"]),peak=float(state["peak"]),close=b.close,
                    initial_stop=float(state["initial_stop"]),holding_days=i-int(state["entry_index"])+1,
                    hard_risk=bool(stock and not stock.eligible),portfolio_drawdown=portfolio_drawdown,
                    extreme_market=getattr(m,"exposure_cap",1.0)<=0,
                    theme_fading=getattr(theme,"lifecycle",None)=="退潮",
                    trend_broken=bool(stock and stock.trend<35),
                    previous_protective=state.get("protective"),
                    max_portfolio_drawdown=max_portfolio_drawdown,
                )
                state["protective"]=decision.protective_price
                if decision.should_exit:
                    pending_exits[sym]={"signal_day":day,"reason":decision.reason,
                                        "priority":decision.priority}
            if (i-warmup)%rebalance_days==0:
                pending={x.symbol:{"weight":x.target_weight,"initial_stop":getattr(x,"initial_stop",None)}
                         for x in p if x.symbol not in pending_exits}
                signal_day=day
    equities=[x["equity"] for x in curve]; daily=[equities[i]/equities[i-1]-1 for i in range(1,len(equities)) if equities[i-1]]
    peak=equities[0]; mdd=0
    for x in equities: peak=max(peak,x); mdd=min(mdd,x/peak-1)
    total=equities[-1]/capital-1; years=max((days[-1]-days[0]).days/365.25,1/252); ann=(equities[-1]/capital)**(1/years)-1
    vol=(sum((x-mean(daily))**2 for x in daily)/max(1,len(daily)-1))**.5*sqrt(252) if daily else 0
    sharpe=mean(daily)*252/vol if vol else 0
    return BacktestResult(capital,round(equities[-1],2),round(total,4),round(ann,4),round(mdd,4),round(vol,4),round(sharpe,2),fills,curve,
                          {"execution":"next trading-day open","commission":.0003,"minimum_commission":5,"stamp_tax_sell":.0005,
                           "slippage_bps":slippage_bps,"lot_size":100,"max_daily_amount_participation":.01,
                           "suspended_limit_constraints":True,"max_portfolio_drawdown":max_portfolio_drawdown,
                           "automatic_trading":False})


def result_dict(result: BacktestResult) -> dict:
    data=asdict(result)
    for f in data["fills"]:
        f["signal_day"]=f["signal_day"].isoformat(); f["fill_day"]=f["fill_day"].isoformat()
    return data
