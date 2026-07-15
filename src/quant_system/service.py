from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import hashlib
import os
from threading import RLock
from uuid import uuid4
from .backtest import result_dict, run_backtest
from .engine import (MODEL_VERSION, assess_market, assess_stocks, assess_themes, build_portfolio,
                     entry_signal, evaluate_exit)
from .models import DataSnapshot, jsonable
from .providers import AkshareProvider, DeterministicDemoProvider, MarketDataProvider, TushareProvider
from .quality import check_quality
from .repository import SQLiteRepository
from zoneinfo import ZoneInfo


@dataclass
class Settings:
    capital: float = 1_000_000
    target_count: int = 4
    include_main: bool = True
    include_chinext: bool = True
    include_star: bool = True
    include_bse: bool = False
    max_portfolio_drawdown: float = .18
    risk_per_trade: float = .0125
    provider: str = "deterministic-demo"
    automatic_trading: bool = False
    notify_eod_success: bool = True
    notify_risk: bool = True
    notification_channel: str = "none"


@dataclass
class QuantService:
    provider: MarketDataProvider = field(default_factory=DeterministicDemoProvider)
    settings: Settings = field(default_factory=Settings)
    snapshot: DataSnapshot | None = None
    latest: dict | None = None
    decisions: list[dict] = field(default_factory=list)
    backtests: dict[str,dict] = field(default_factory=dict)
    repository: SQLiteRepository = field(default_factory=lambda: SQLiteRepository(os.getenv("QUANT_DB_PATH","data/quant_system.db")))
    _eod_lock: RLock = field(default_factory=RLock,repr=False)

    def __post_init__(self):
        persisted = self.repository.load_settings()
        if persisted:
            allowed = set(self.settings.__dataclass_fields__) - {"automatic_trading"}
            for key, value in persisted.items():
                if key in allowed:
                    setattr(self.settings, key, value)
        self.settings.automatic_trading = False
        self.decisions=self.repository.list_decisions(100)
        self.backtests={x["id"]:x for x in self.repository.list_backtests() if "id" in x}
        if self.decisions:
            self.latest=self.decisions[0].get("snapshot")
            if not self.latest or "portfolio_status" not in self.latest or any(p.get("model_version")!=MODEL_VERSION for p in self.latest.get("portfolio",[])):
                self.latest=None

    def update_settings(self, changes: dict, *, actor: str = "product-api") -> dict:
        previous_capital = self.settings.capital
        if "capital" in changes and float(changes["capital"]) != previous_capital:
            self.repository.reconfigure_simulation_account(float(changes["capital"]))
        for key, value in changes.items():
            if key == "automatic_trading":
                continue
            if not hasattr(self.settings, key):
                raise ValueError(f"unsupported setting {key}")
            setattr(self.settings, key, value)
        self.settings.automatic_trading = False
        payload = jsonable(self.settings)
        saved = self.repository.save_settings(payload, actor=actor)
        self.latest = None
        return saved

    @staticmethod
    def _next_trading_time(as_of: datetime, declared_next_day: str | date | None = None) -> datetime:
        if declared_next_day is not None:
            try:
                day = declared_next_day if isinstance(declared_next_day, date) else date.fromisoformat(str(declared_next_day))
            except ValueError:
                day = None
            if day is not None and day > as_of.date():
                return datetime.combine(day, time(9, 30), ZoneInfo("Asia/Shanghai"))
        day=as_of.date()+timedelta(days=1)
        while day.weekday()>=5:day+=timedelta(days=1)
        return datetime.combine(day,time(9,30),ZoneInfo("Asia/Shanghai"))

    def _scoped_snapshot(self, snapshot: DataSnapshot) -> DataSnapshot:
        def allowed(board: str) -> bool:
            normalized=(board or "主板").strip()
            if "北交" in normalized:return self.settings.include_bse
            if "创业" in normalized:return self.settings.include_chinext
            if "科创" in normalized:return self.settings.include_star
            return self.settings.include_main

        all_symbols={bar.symbol for bar in snapshot.bars}
        bars=[bar for bar in snapshot.bars if allowed(bar.board)]
        scoped_symbols={bar.symbol for bar in bars}
        expected=(round(snapshot.expected_symbols*len(scoped_symbols)/len(all_symbols))
                  if all_symbols else snapshot.expected_symbols)
        metadata={**snapshot.metadata,"universe_scope":{
            "include_main":self.settings.include_main,
            "include_chinext":self.settings.include_chinext,
            "include_star":self.settings.include_star,
            "include_bse":self.settings.include_bse,
            "symbols_before":len(all_symbols),"symbols_after":len(scoped_symbols),
        }}
        return DataSnapshot(snapshot.as_of,bars,snapshot.provider,expected,metadata)

    def _buffered_portfolio(self, snapshot, market, stocks, portfolio_drawdown: float = 0.0):
        """Apply a one-normal-replacement-per-ISO-week buffer using current assessments only."""
        previous=self.decisions[0] if self.decisions else None
        previous_snapshot=previous.get("snapshot",{}) if previous else {}
        old_symbols=[p["symbol"] for p in previous_snapshot.get("portfolio",[])]
        # A model version/config discontinuity starts a fresh lineage rather than mixing rules.
        if previous and previous.get("model_version")!=MODEL_VERSION:
            old_symbols=[];previous=None
        current_week=snapshot.as_of.date().isocalendar()[:2]
        previous_week=None
        if previous:
            previous_week=datetime.fromisoformat(previous["data_timestamp"]).date().isocalendar()[:2]
        prior_turnover=previous.get("turnover",{}) if previous else {}
        week_used=int(prior_turnover.get("week_normal_replacements_used",0)) if previous_week==current_week else 0
        replacement_budget=max(0,1-week_used)
        by_symbol={s.symbol:s for s in stocks};eligible=[s for s in stocks if s.eligible]
        histories={}
        for bar in snapshot.bars:histories.setdefault(bar.symbol,[]).append(bar)
        for values in histories.values():values.sort(key=lambda item:item.day)
        previous_positions={item["symbol"]:item for item in previous_snapshot.get("portfolio",[])}
        position_states={};risk_exits=[]
        rank={s.symbol:(i+1)/max(1,len(eligible)) for i,s in enumerate(eligible)}
        hard=[];normal_candidates=[];retained=[]
        for symbol in old_symbols:
            stock=by_symbol.get(symbol)
            prior=previous_positions.get(symbol,{})
            symbol_bars=histories.get(symbol,[])
            if stock is None or not symbol_bars:
                hard.append({"symbol":symbol,"reason":"当前证券池或行情缺失","kind":"hard_risk"})
                continue
            if not stock.eligible:
                hard.append({"symbol":symbol,"reason":stock.excluded_reason or "当前证券硬风险门禁失败","kind":"hard_risk"})
                continue
            latest=symbol_bars[-1]
            entry=float(prior.get("entry_price") or latest.close)
            peak=max(float(prior.get("highest_price") or entry),latest.high)
            initial_stop=float(prior.get("initial_stop") or entry*.90)
            raw_entry_at=prior.get("entry_at") or prior.get("data_timestamp") or snapshot.as_of.isoformat()
            try:entry_at=datetime.fromisoformat(str(raw_entry_at))
            except ValueError:entry_at=snapshot.as_of
            holding_days=sum(1 for bar in symbol_bars if bar.day>=entry_at.date())
            exit_decision=evaluate_exit(
                entry=entry,peak=peak,close=latest.close,initial_stop=initial_stop,
                holding_days=holding_days,hard_risk=not stock.eligible,
                portfolio_drawdown=portfolio_drawdown,
                extreme_market=market.exposure_cap<=0,
                theme_fading=stock.theme_lifecycle=="退潮",
                trend_broken=stock.trend<35,
                previous_protective=prior.get("protective_price"),
                max_portfolio_drawdown=self.settings.max_portfolio_drawdown,
            )
            position_states[symbol]={"entry_price":entry,"highest_price":peak,
                "initial_stop":initial_stop,"protective_price":exit_decision.protective_price,
                "entry_at":entry_at,"holding_days":holding_days,
                "entry_state":prior.get("entry_state","持仓复核")}
            if exit_decision.should_exit:
                risk_exits.append({"symbol":symbol,"reason":exit_decision.reason,
                                   "kind":"risk_exit","priority":exit_decision.priority})
            elif rank.get(symbol,1)>.30:
                normal_candidates.append({"symbol":symbol,"reason":f"当前排名 {rank[symbol]*100:.1f}%，跌出前30%","kind":"normal"})
            else:
                retained.append(symbol)
        if portfolio_drawdown <= -abs(self.settings.max_portfolio_drawdown):
            replaced=[{"symbol":symbol,
                       "reason":f"组合达到{abs(self.settings.max_portfolio_drawdown)*100:.0f}%硬风控",
                       "kind":"risk_exit","priority":2} for symbol in old_symbols]
            turnover={"replacement_budget":replacement_budget,"week_normal_replacements_used":week_used,
                      "retained":[],"replaced":replaced,"added":[],"exception":"portfolio_drawdown_risk_off"}
            return [],turnover
        normal_replaced=normal_candidates[:replacement_budget]
        retained.extend(x["symbol"] for x in normal_candidates[replacement_budget:])
        if market.exposure_cap<=0:
            replaced=[{"symbol":s,"reason":"市场 risk_off/极端风险","kind":"market_risk"} for s in old_symbols]
            turnover={"replacement_budget":replacement_budget,"week_normal_replacements_used":week_used,
                      "retained":[],"replaced":replaced,"added":[],"exception":"risk_off"}
            return [],turnover
        replaced=hard+risk_exits+normal_replaced
        initialization=len(old_symbols)==0
        recovery=len(old_symbols)<3 or (len(retained)<3 and bool(hard))
        if initialization:desired=max(3,min(5,self.settings.target_count))
        elif recovery:desired=3
        else:desired=max(3,len(old_symbols)-len(hard))
        # One normal removal permits at most one normal addition. Hard-risk recovery may fill to three.
        add_slots=max(0,desired-len(retained))
        exited_symbols={item["symbol"] for item in hard+risk_exits}
        ordered=[by_symbol[s] for s in retained if s in by_symbol]
        ordered.extend(s for s in stocks if s.symbol not in set(retained) and s.symbol not in exited_symbols)
        portfolio=build_portfolio(snapshot,market,ordered,self.settings.capital,desired,self.settings.risk_per_trade,allow_low_score_symbols=set(retained))
        final_symbols=[p.symbol for p in portfolio]
        final_retained=[s for s in retained if s in final_symbols]
        added=[s for s in final_symbols if s not in old_symbols]
        # If constraints unexpectedly evict a buffered holding, do not silently exceed the weekly budget.
        constraint_evictions=[s for s in retained if s not in final_symbols]
        if constraint_evictions and not recovery:
            allowed=set(x["symbol"] for x in normal_replaced)
            if any(s not in allowed for s in constraint_evictions):
                # Fail closed to the prior valid holdings by rebuilding from retained current assessments only.
                portfolio=build_portfolio(snapshot,market,[by_symbol[s] for s in retained if s in by_symbol],self.settings.capital,max(3,len(retained)),self.settings.risk_per_trade,allow_low_score_symbols=set(retained))
                final_symbols=[p.symbol for p in portfolio];final_retained=[s for s in retained if s in final_symbols];added=[]
        for p in portfolio:
            if p.symbol in final_retained:
                state=position_states[p.symbol];p.action="持有";p.entry_state=state["entry_state"]
                p.entry_price=state["entry_price"];p.highest_price=round(state["highest_price"],2)
                p.initial_stop=state["initial_stop"];p.protective_price=state["protective_price"]
                p.entry_at=state["entry_at"]
        used_now=len(normal_replaced)
        exception="initialization" if initialization else "recovery_to_three" if recovery else None
        turnover={"replacement_budget":replacement_budget,"week_normal_replacements_used":week_used+used_now,
                  "retained":[{"symbol":s,"reason":"仍在当前合格前30%，使用持仓缓冲"} for s in final_retained],
                  "replaced":replaced,"added":[{"symbol":s,"reason":"当前重新评分后入选"} for s in added],"exception":exception}
        return portfolio,turnover

    def run_eod(self, as_of: date | None=None, *, enforce_freshness=False, run_key: str|None=None) -> dict:
        with self._eod_lock:
            return self._run_eod(as_of,enforce_freshness=enforce_freshness,run_key=run_key)

    def _run_eod(self, as_of: date | None=None, *, enforce_freshness=False, run_key: str|None=None) -> dict:
        requested=(as_of or (date(2026,7,3) if self.provider.name==DeterministicDemoProvider.name else date.today()))
        config=f"{self.settings.capital}:{self.settings.target_count}:{self.settings.risk_per_trade}:{self.settings.include_main}:{self.settings.include_chinext}:{self.settings.include_star}:{self.settings.include_bse}"
        run_key=run_key or hashlib.sha256(f"{self.provider.name}:{requested}:{MODEL_VERSION}:{config}".encode()).hexdigest()[:24]
        previous=self.repository.get_run(run_key)
        if previous is not None:
            if previous.get("published") or previous.get("displayable"):
                self.latest=previous
            previous={**previous,"idempotent_replay":True}
            return previous
        raw_snapshot=self.provider.load(requested)
        self.snapshot=self._scoped_snapshot(raw_snapshot)
        self.repository.ensure_simulation_account(self.settings.capital)
        raw_latest_day=max(b.day for b in raw_snapshot.bars) if raw_snapshot.bars else None
        ledger_bars={b.symbol:b for b in raw_snapshot.bars if b.day==raw_latest_day}
        latest_day=max(b.day for b in self.snapshot.bars) if self.snapshot.bars else None
        latest_bars={b.symbol:b for b in self.snapshot.bars if b.day==latest_day}
        q=check_quality(self.snapshot)
        effective_gate=enforce_freshness or self.provider.name != DeterministicDemoProvider.name
        error_codes={issue.code for issue in q.issues if issue.severity=="error"}
        observation_requested=bool(self.snapshot.metadata.get("observation_only",False) or
                                   self.snapshot.metadata.get("public_data",False))
        # Public interfaces may drive a forward-only paper observation when
        # their sole blocking issue is the intentionally retained
        # NOT_PRODUCTION_READY gate.  Any freshness, coverage, OHLC or required
        # dataset error still fails closed.  This never upgrades the source to
        # PIT/production or relaxes the research gates.
        observation_mode=observation_requested and error_codes=={"NOT_PRODUCTION_READY"}
        # Historical demo dates may intentionally be stale, but structural
        # errors (empty scope, invalid OHLC, missing required data) must never
        # enter the scoring engine, even in demo mode.
        demo_relaxable_errors={"STALE"}
        should_block=(q.status=="blocked" and not observation_mode and
                      (effective_gate or not error_codes.issubset(demo_relaxable_errors)))
        matched=[]
        if should_block:
            valuation=self.repository.mark_to_market(self.snapshot.as_of.date().isoformat(),{s:b.close for s,b in ledger_bars.items()},{"run_key":run_key,"quality":"blocked","matched":matched})
            blocked={"published":False,"run_key":run_key,"quality":jsonable(q),"message":"数据质量门禁未通过，保留上一版建议并标记过期","last_published":self.latest,"simulation":{"matched":matched,"valuation":valuation}}
            self.repository.save_run(run_key,"blocked",blocked)
            return blocked
        matching_ready=(not observation_requested or
                        bool(self.snapshot.metadata.get("simulation_matching_ready",False)))
        # Matching happens only after the quality gate. Existing positions and
        # pending exits still use full-market bars so a board-scope change
        # cannot value an excluded holding at zero.
        if matching_ready:
            matched=self.repository.match_pending(raw_snapshot.as_of.isoformat(),ledger_bars)
        valuation=self.repository.mark_to_market(
            self.snapshot.as_of.date().isoformat(),
            {s:b.close for s,b in ledger_bars.items()},
            {"run_key":run_key,"matched":matched,"broker_connected":False},
        )
        market=assess_market(self.snapshot); themes=assess_themes(self.snapshot); stocks=assess_stocks(self.snapshot,themes,self.settings.capital)
        portfolio,turnover=self._buffered_portfolio(self.snapshot,market,stocks,valuation["drawdown"])
        portfolio_condition="risk_off" if market.exposure_cap<=0 else ("partial" if len(portfolio)<3 else "healthy")
        portfolio_status=("observation" if observation_mode and portfolio_condition=="healthy"
                          else portfolio_condition)
        portfolio_reason=("市场处于极端风险状态，保持现金" if portfolio_condition=="risk_off" else
                          "合格股票不足3只，宁可持有现金也不强行补足" if portfolio_condition=="partial" else
                          "组合满足数量、题材、相关性、产业链与容量约束")
        if observation_mode:
            portfolio_reason=f"公开数据前瞻观察（底层组合状态：{portfolio_condition}）；不得作为生产建议"
        quality_view=jsonable(q)
        if observation_mode: quality_view["status"]="observation_only"
        release_mode=("observation_only" if observation_mode else
                      "demo" if self.provider.name==DeterministicDemoProvider.name else "production")
        provenance_keys=("public_data","observation_only","production_ready","pit_verified",
                         "pit_reconstruction","research_eligible","theme_mapping","enrichments",
                         "security_metadata","price_history","data_quality","market_inputs",
                         "universe_selection")
        data_provenance=jsonable({key:self.snapshot.metadata[key] for key in provenance_keys
                                  if key in self.snapshot.metadata})
        holding_symbols={item.symbol for item in portfolio}
        backups=[item for item in stocks if item.eligible and item.symbol not in holding_symbols][:3]
        history_by_symbol={}
        for bar in self.snapshot.bars:history_by_symbol.setdefault(bar.symbol,[]).append(bar)
        signal_qualified=sum(1 for item in stocks if item.eligible and
                             entry_signal(sorted(history_by_symbol[item.symbol],key=lambda bar:bar.day)) is not None)
        selection_funnel={"universe":len(stocks),"security_eligible":sum(1 for item in stocks if item.eligible),
                          "theme_entry_qualified":sum(1 for item in stocks if item.eligible and item.gate_results.get("theme_lifecycle",{}).get("passed")),
                          "signal_qualified":signal_qualified,"selected":len(portfolio),"backups":len(backups)}
        decision_id=str(uuid4()); result={"decision_id":decision_id,"run_key":run_key,
          "published":not observation_mode,"displayable":True,
          "production_published":release_mode=="production","release_mode":release_mode,
          "as_of":self.snapshot.as_of.isoformat(),"data_provenance":data_provenance,
          "provider":self.snapshot.provider,"quality":quality_view,"market":jsonable(market),"themes":jsonable(themes),
          "portfolio":jsonable(portfolio),"candidates":jsonable(backups),"selection_funnel":selection_funnel,
          "cash_weight":round(1-sum(x.target_weight for x in portfolio),4),"portfolio_status":portfolio_status,"portfolio_reason":portfolio_reason,"model_portfolio_only":True,
          "portfolio_condition":portfolio_condition,
          "research_eligible":False if observation_mode else bool(self.snapshot.metadata.get("research_eligible",False)),
          "turnover":turnover,
          "disclaimer":"仅供研究与决策辅助，不构成收益承诺；系统不连接券商、不自动交易。"}
        audit={"id":decision_id,"timestamp":datetime.now().astimezone().isoformat(),"data_timestamp":self.snapshot.as_of.isoformat(),
               "model_version":MODEL_VERSION,"provider":self.snapshot.provider,"market_regime":market.regime,
               "release_mode":release_mode,"production_published":result["production_published"],
               "research_eligible":result["research_eligible"],
               "holdings":[x.symbol for x in portfolio],"reasons":list(market.reasons),"snapshot":result}
        audit["turnover"]=turnover
        audit=jsonable(audit);self.repository.save_decision(audit,run_key)
        effective=self._next_trading_time(
            self.snapshot.as_of,
            self.snapshot.metadata.get("next_trading_day") if isinstance(self.snapshot.metadata,dict) else None,
        ).isoformat()
        held=self.repository.simulation_positions();desired={p["symbol"]:p for p in result["portfolio"]};intents=[]
        if matching_ready:
            for symbol,position in held.items():
                if symbol not in desired:
                    intents.append({"symbol":symbol,"side":"sell","quantity":position["shares"],"target_weight":0,"initial_weight":0})
            for symbol,p in desired.items():
                current_value=held.get(symbol,{}).get("shares",0)*latest_bars[symbol].close
                amount=max(0,p["initial_weight"]*valuation["equity"]-current_value)
                if amount>=latest_bars[symbol].close*100:
                    intents.append({"symbol":symbol,"side":"buy","amount":round(amount,2),"target_weight":p["target_weight"],"initial_weight":p["initial_weight"]})
            self.repository.append_simulation_intents(run_key,self.snapshot.as_of.isoformat(),effective,intents)
        result["simulation"]={"matched":matched,"valuation":valuation,"new_intents":intents,
                              "matching_ready":matching_ready,
                              "matching_reason":None if matching_ready else "公开源缺少完整涨跌停/停牌约束，仅展示决策观察，不生成或撮合模拟指令",
                              "broker_connected":False}
        self.repository.save_run(run_key,"observation" if observation_mode else "published",result)
        self.decisions.insert(0,audit); self.latest=result
        return result

    def ensure(self, require_snapshot: bool = False):
        if self.latest is None:
            result=self.run_eod()
            if result.get("published") or result.get("displayable"):
                self.latest=result
            elif result.get("last_published"):
                self.latest=result["last_published"]
            else:
                # A previously persisted blocked run must not poison all read
                # endpoints after a restart/test reset. Fall back to the most
                # recent immutable published decision, while its own quality
                # timestamp remains available for the UI's stale indication.
                persisted=self.repository.list_decisions(1)
                if persisted:
                    self.latest=persisted[0].get("snapshot")
        if require_snapshot and self.snapshot is None:
            as_of=date.fromisoformat(self.latest["as_of"][:10]) if self.latest else None
            self.snapshot=self._scoped_snapshot(self.provider.load(as_of))
        return self.latest

    def run_backtest(self, capital:float|None=None)->tuple[str,dict]:
        self.ensure(require_snapshot=True); ident=str(uuid4()); result=result_dict(run_backtest(
            self.snapshot,capital or self.settings.capital,
            max_portfolio_drawdown=self.settings.max_portfolio_drawdown)); result["id"]=ident; result["status"]="completed"
        self.backtests[ident]=result; self.repository.save_backtest(ident,result); return ident,result

    def provider_statuses(self):
        defaults=[DeterministicDemoProvider().status(),TushareProvider().status(),AkshareProvider().status()]
        active=self.provider.status()
        return [active if item.get("provider")==self.provider.name else item for item in defaults]

    def simulation(self): return self.repository.simulation()
