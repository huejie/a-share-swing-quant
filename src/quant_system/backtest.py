from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date
from math import sqrt
from statistics import mean
from .models import Bar, DataSnapshot
from .engine import assess_market, assess_themes, assess_stocks, build_portfolio


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
                 slippage_bps: float=8.0, active_factors: frozenset[str] | None=None) -> BacktestResult:
    """Daily event loop: close signal, next trading-day open execution, no same-day hindsight."""
    days=sorted({b.day for b in snapshot.bars}); by_day={d:{} for d in days}
    for b in snapshot.bars: by_day[b.day][b.symbol]=b
    cash=capital; holdings:dict[str,int]={}; fills=[]; curve=[]; pending:dict[str,float]|None=None; signal_day=None
    warmup=60
    for i,day in enumerate(days):
        bars=by_day[day]
        if pending is not None:
            # liquidate names no longer desired; blocked on suspended/limit-down
            for sym in list(holdings):
                if sym not in pending and sym in bars and not bars[sym].suspended and not bars[sym].limit_down:
                    b=bars[sym]; px=b.open*(1-slippage_bps/10000); qty=holdings.pop(sym); value=px*qty; fee=_cost(value,"sell"); cash+=value-fee
                    fills.append(Fill(signal_day,day,sym,"sell",round(px,2),qty,round(fee,2)))
            equity=cash+sum(qty*bars[s].open for s,qty in holdings.items() if s in bars)
            for sym,weight in pending.items():
                if sym in holdings or sym not in bars: continue
                b=bars[sym]
                if b.suspended or b.limit_up: continue
                px=b.open*(1+slippage_bps/10000); max_value=min(equity*weight,b.amount*.01); qty=int(max_value/px/100)*100
                if qty<=0: continue
                value=qty*px; fee=_cost(value,"buy")
                if value+fee<=cash: cash-=value+fee; holdings[sym]=qty; fills.append(Fill(signal_day,day,sym,"buy",round(px,2),qty,round(fee,2)))
            pending=None
        close_equity=cash+sum(qty*bars[s].close for s,qty in holdings.items() if s in bars)
        curve.append({"date":day.isoformat(),"equity":round(close_equity,2),"cash":round(cash,2),"positions":len(holdings)})
        if i>=warmup and (i-warmup)%rebalance_days==0:
            subbars=[b for b in snapshot.bars if b.day<=day]
            sub=DataSnapshot(snapshot.as_of,subbars,snapshot.provider,snapshot.expected_symbols,snapshot.metadata)
            m=assess_market(sub,active_factors); t=assess_themes(sub,active_factors); s=assess_stocks(sub,t,close_equity,active_factors); p=build_portfolio(sub,m,s,close_equity,active_factors=active_factors)
            pending={x.symbol:x.target_weight for x in p}; signal_day=day
    equities=[x["equity"] for x in curve]; daily=[equities[i]/equities[i-1]-1 for i in range(1,len(equities)) if equities[i-1]]
    peak=equities[0]; mdd=0
    for x in equities: peak=max(peak,x); mdd=min(mdd,x/peak-1)
    total=equities[-1]/capital-1; years=max((days[-1]-days[0]).days/365.25,1/252); ann=(equities[-1]/capital)**(1/years)-1
    vol=(sum((x-mean(daily))**2 for x in daily)/max(1,len(daily)-1))**.5*sqrt(252) if daily else 0
    sharpe=mean(daily)*252/vol if vol else 0
    return BacktestResult(capital,round(equities[-1],2),round(total,4),round(ann,4),round(mdd,4),round(vol,4),round(sharpe,2),fills,curve,
                          {"execution":"next trading-day open","commission":.0003,"minimum_commission":5,"stamp_tax_sell":.0005,
                           "slippage_bps":slippage_bps,"lot_size":100,"max_daily_amount_participation":.01,
                           "suspended_limit_constraints":True,"automatic_trading":False})


def result_dict(result: BacktestResult) -> dict:
    data=asdict(result)
    for f in data["fills"]:
        f["signal_day"]=f["signal_day"].isoformat(); f["fill_day"]=f["fill_day"].isoformat()
    return data
