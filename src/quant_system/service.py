from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from threading import RLock
from uuid import uuid4
from .backtest import result_dict, run_backtest
from .engine import MODEL_VERSION, assess_market, assess_stocks, assess_themes, entry_signal
from .models import DataSnapshot, jsonable
from .notifications import NotificationDispatcher
from .portfolio_policy import buffered_portfolio, weekly_theme_names
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
    max_adv_participation: float = .02
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
    settings_version: int = 0
    _eod_lock: RLock = field(default_factory=RLock,repr=False)

    def __post_init__(self):
        persisted = self.repository.load_settings()
        if persisted:
            self.settings_version=int(persisted.get("settings_version",0))
            allowed = set(self.settings.__dataclass_fields__) - {"automatic_trading","provider"}
            for key, value in persisted.items():
                if key in allowed:
                    setattr(self.settings, key, value)
        self.settings.automatic_trading = False
        self.settings.provider=self.provider.name
        self.decisions=self.repository.list_decisions(100)
        self.backtests={x["id"]:x for x in self.repository.list_backtests() if "id" in x}
        if self.decisions:
            latest_decision=self.decisions[0]
            self.latest=latest_decision.get("snapshot")
            if (self.latest and latest_decision.get("model_version")==MODEL_VERSION and
                    "model_version" not in self.latest):
                # Backward-compatible read of snapshots written before the
                # dashboard exposed its model version at the top level.
                self.latest={**self.latest,"model_version":MODEL_VERSION}
            if (not self.latest or "portfolio_status" not in self.latest or
                    "selected_theme_names" not in self.latest or
                    self.latest.get("model_version")!=MODEL_VERSION or
                    any(p.get("model_version")!=MODEL_VERSION for p in self.latest.get("portfolio",[]))):
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
        self.settings_version=int(saved["settings_version"])
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

    @staticmethod
    def _runtime_freshness_view(latest: dict | None, now: datetime | None = None) -> dict | None:
        """Age a persisted decision at read time without rewriting its audit record."""
        if not latest or not latest.get("as_of"):
            return latest
        now=now or datetime.now(ZoneInfo("Asia/Shanghai"))
        try:
            as_of=datetime.fromisoformat(str(latest["as_of"]))
        except (TypeError,ValueError):
            return latest
        if as_of.tzinfo is None:
            as_of=as_of.replace(tzinfo=now.tzinfo)
        age=max(0.0,(now-as_of.astimezone(now.tzinfo)).total_seconds()/3600)
        quality=dict(latest.get("quality") or {})
        if age<=72 or quality.get("freshness")=="stale":
            return latest
        issues=list(quality.get("issues") or [])
        if not any(item.get("code")=="STALE" for item in issues if isinstance(item,dict)):
            issues.append({"code":"STALE","severity":"error",
                           "message":f"数据距今 {age:.1f} 小时，禁止作为当前建议"})
        quality.update({"freshness":"stale","status":"blocked","age_hours":round(age,1),"issues":issues})
        return {**latest,"quality":quality}

    @staticmethod
    def _snapshot_hash(snapshot: DataSnapshot) -> str:
        volatile={"collected_at","collection_time","generated_at","retrieved_at","fetched_at"}
        def stable(value):
            if isinstance(value,dict):
                return {key:stable(item) for key,item in sorted(value.items()) if key not in volatile}
            if isinstance(value,list):return [stable(item) for item in value]
            return value
        bars=sorted((jsonable(bar) for bar in snapshot.bars),key=lambda item:(item["symbol"],item["day"]))
        payload={"provider":snapshot.provider,"as_of":snapshot.as_of.isoformat(),
                 "expected_symbols":snapshot.expected_symbols,"bars":bars,
                 "metadata":stable(jsonable(snapshot.metadata))}
        encoded=json.dumps(payload,ensure_ascii=False,separators=(",",":"),sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()

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
        previous = self.decisions[0] if self.decisions else None
        return buffered_portfolio(
            snapshot, market, stocks, previous_decision=previous,
            capital=self.settings.capital, target_count=self.settings.target_count,
            risk_per_trade=self.settings.risk_per_trade,
            max_adv_participation=self.settings.max_adv_participation,
            max_drawdown=self.settings.max_portfolio_drawdown,
            portfolio_drawdown=portfolio_drawdown,
        )

    def _exit_actions(self, snapshot:DataSnapshot, stocks:list, turnover:dict)->list[dict]:
        previous=self.decisions[0].get("snapshot",{}) if self.decisions else {}
        prior_by={item.get("symbol"):item for item in previous.get("portfolio",[]) if item.get("symbol")}
        stock_by={item.symbol:item for item in stocks}
        latest_by={}
        for bar in snapshot.bars:
            if bar.day==snapshot.as_of.date() or bar.symbol not in latest_by or bar.day>latest_by[bar.symbol].day:
                latest_by[bar.symbol]=bar
        actions=[]
        for replacement in turnover.get("replaced",[]):
            symbol=replacement.get("symbol");prior=prior_by.get(symbol,{})
            stock=stock_by.get(symbol);bar=latest_by.get(symbol)
            if not symbol:continue
            actions.append({
                "symbol":symbol,"name":prior.get("name") or getattr(stock,"name",None) or (bar.name if bar else symbol),
                "theme":prior.get("theme") or getattr(stock,"theme",None) or (bar.theme if bar else "未分类"),
                "action":"退出","target_weight":0.0,
                "initial_weight":0.0,"current_weight":prior.get("target_weight",prior.get("current_weight",0.0)),
                "entry_price":prior.get("entry_price",bar.close if bar else 0.0),
                "initial_stop":prior.get("initial_stop",0.0),"protective_price":prior.get("protective_price"),
                "highest_price":prior.get("highest_price",bar.high if bar else 0.0),
                "entry_state":"退出触发","trigger_zone":[],"score":getattr(stock,"score",prior.get("score",0.0)),
                "thesis":[replacement.get("reason") or "退出条件触发"],
                "invalidation":replacement.get("reason") or "退出条件触发",
                "risk_notes":["模型退出动作；次日可成交且受停牌、跌停和流动性约束"],
                "expected_holding_days":[40,80],"next_review_at":snapshot.as_of.isoformat(),
                "model_version":MODEL_VERSION,"data_timestamp":snapshot.as_of.isoformat(),
                "exit_priority":replacement.get("priority"),"exit_reason":replacement.get("reason"),
                "exit_kind":replacement.get("kind"),
            })
        return actions

    def _weekly_theme_names(self,snapshot:DataSnapshot,themes:list)->list[str]:
        previous=self.decisions[0] if self.decisions else None
        return weekly_theme_names(snapshot,themes,previous)

    def _notify(self, *, run_key: str, event_type: str, payload: dict) -> dict | None:
        """Best-effort notification boundary; never alters an EOD outcome."""
        try:
            return NotificationDispatcher(self.repository).emit(
                event_key=f"{run_key}:{event_type}",
                event_type=event_type,
                channel=self.settings.notification_channel,
                payload={"run_key": run_key, "model_version": MODEL_VERSION,
                         "provider": self.provider.name, **payload},
            )
        except Exception:
            # Persistence and delivery are deliberately secondary to the
            # immutable decision/run result. The dispatcher stores safe
            # failures whenever the repository remains available.
            return None

    @staticmethod
    def _terminal_simulation_symbols(snapshot: DataSnapshot, latest_day: date | None,
                                     held: dict[str,dict]) -> dict[str,str]:
        """Return only positions with affirmative permanent-unpriceable evidence."""
        if latest_day is None:return {}
        histories:dict[str,list]= {}
        for bar in snapshot.bars:
            histories.setdefault(bar.symbol,[]).append(bar)
        explicit=set(snapshot.metadata.get("delisted_symbols",[]) if isinstance(snapshot.metadata,dict) else [])
        result={}
        for symbol in held:
            history=sorted(histories.get(symbol,[]),key=lambda bar:bar.day)
            if symbol in explicit:
                result[symbol]="provider_confirmed_delisted"
            elif history and history[-1].day<latest_day and history[-1].is_delisting:
                result[symbol]="delisting_flag_then_permanent_quote_absence"
            elif (history and history[-1].day<latest_day-timedelta(days=20)
                  and not history[-1].suspended):
                result[symbol]="quote_absent_over_20_calendar_days_without_suspension"
            elif history and history[-1].suspended and history[-1].day<latest_day-timedelta(days=60):
                result[symbol]="suspension_quote_stale_over_60_calendar_days"
        return result

    @staticmethod
    def _simulation_execution_plan(portfolio:list,exit_actions:list[dict],latest_bars:dict,
                                   held:dict[str,dict],equity:float)->tuple[list[dict],list[dict],dict]:
        """Translate model targets into deltas against the actual paper account."""
        intents=[];payloads=[];desired_symbols=set()
        for advice in portfolio:
            item=jsonable(advice);symbol=item["symbol"];desired_symbols.add(symbol)
            position=held.get(symbol,{});shares=int(position.get("shares",0));bar=latest_bars.get(symbol)
            price=float(bar.close) if bar is not None else 0.0
            current_value=shares*price;current_weight=current_value/equity if equity>0 else 0.0
            target=float(item["target_weight"])
            execution_target=float(item["initial_weight"] if shares<=0 else target)
            delta=execution_target*equity-current_value
            lot_value=price*100
            if shares<=0:
                action="待买";stage="initial"
            elif delta>=lot_value>0:
                action="加仓";stage="add_confirmation"
            elif delta<=-lot_value<0:
                action="减仓";stage="target_reduction"
            else:
                action="持有";stage="hold"
            item.update({"action":action,"current_weight":round(current_weight,4),
                         "model_target_weight":target,"execution_target_weight":round(execution_target,4),
                         "execution_stage":stage,"simulated_shares":shares})
            payloads.append(item)
            common={"symbol":symbol,"target_weight":target,"initial_weight":float(item["initial_weight"]),
                    "current_weight":round(current_weight,6),"execution_target_weight":execution_target,
                    "model_action":action,"stage":stage}
            if price<=0:continue
            if delta>=lot_value:
                intents.append({**common,"side":"buy","amount":round(delta,2)})
            elif delta<=-lot_value:
                quantity=int((-delta/price)//100)*100
                if quantity>0:intents.append({**common,"side":"sell","quantity":min(shares,quantity)})
        for symbol,position in held.items():
            if symbol in desired_symbols:continue
            shares=int(position["shares"]);bar=latest_bars.get(symbol);price=float(bar.close) if bar else 0.0
            weight=shares*price/equity if equity>0 else 0.0
            intents.append({"symbol":symbol,"side":"sell","quantity":shares,"target_weight":0.0,
                            "initial_weight":0.0,"current_weight":round(weight,6),
                            "execution_target_weight":0.0,"model_action":"退出","stage":"exit"})
        by_exit={item.get("symbol"):item for item in exit_actions}
        for symbol,item in by_exit.items():
            position=held.get(symbol,{});bar=latest_bars.get(symbol);shares=int(position.get("shares",0))
            current=shares*(float(bar.close) if bar else 0.0)/equity if equity>0 else 0.0
            item.update({"current_weight":round(current,4),"simulated_shares":shares,
                         "model_action":"退出","execution_target_weight":0.0})
        state={"account_equity":round(equity,2),"actual_position_count":len(held),
               "intent_count":len(intents),"basis":"actual_simulated_shares_vs_model_execution_target"}
        return payloads,intents,state

    def run_eod(self, as_of: date | None=None, *, enforce_freshness=False, run_key: str|None=None) -> dict:
        with self._eod_lock:
            return self._run_eod(as_of,enforce_freshness=enforce_freshness,run_key=run_key)

    def _run_eod(self, as_of: date | None=None, *, enforce_freshness=False, run_key: str|None=None) -> dict:
        requested=(as_of or (date(2026,7,3) if self.provider.name==DeterministicDemoProvider.name else date.today()))
        config_payload={key:getattr(self.settings,key) for key in (
            "capital","target_count","max_portfolio_drawdown","risk_per_trade","max_adv_participation",
            "include_main","include_chinext","include_star","include_bse")}
        config_payload["settings_version"]=self.settings_version
        config_encoded=json.dumps(config_payload,ensure_ascii=False,separators=(",",":"),sort_keys=True)
        config_hash=hashlib.sha256(config_encoded.encode()).hexdigest()
        if run_key is not None:
            previous=self.repository.get_run(run_key)
            if previous is not None:
                previous={**previous,"model_version":previous.get("model_version",MODEL_VERSION)}
                if previous.get("published") or previous.get("displayable"):
                    self.latest=previous
                return {**previous,"idempotent_replay":True}
        raw_snapshot=self.provider.load(requested)
        data_snapshot_hash=self._snapshot_hash(raw_snapshot)
        simulation_lineage={"model_version":MODEL_VERSION,"provider":raw_snapshot.provider,
                            "config_hash":config_hash,"settings_version":self.settings_version,
                            "data_snapshot_hash":data_snapshot_hash}
        self.repository.save_input_snapshot(data_snapshot_hash,jsonable(raw_snapshot))
        if run_key is None:
            run_key=hashlib.sha256(
                f"{self.provider.name}:{requested}:{MODEL_VERSION}:{config_hash}:{data_snapshot_hash}".encode()
            ).hexdigest()[:24]
            previous=self.repository.get_run(run_key)
            if previous is not None:
                previous={**previous,"model_version":previous.get("model_version",MODEL_VERSION)}
                if previous.get("published") or previous.get("displayable"):
                    self.latest=previous
                return {**previous,"idempotent_replay":True}
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
            valuation=self.repository.mark_to_market(self.snapshot.as_of.date().isoformat(),{s:b.close for s,b in ledger_bars.items()},
                {"run_key":run_key,"quality":"blocked","matched":matched,
                 **simulation_lineage,"matching_ready":False})
            blocked={"published":False,"run_key":run_key,"model_version":MODEL_VERSION,
                     "config_version":self.settings_version,"config_hash":config_hash,
                     "data_snapshot_hash":data_snapshot_hash,"quality":jsonable(q),
                     "as_of":self.snapshot.as_of.isoformat(),"provider":self.snapshot.provider,
                     "message":"数据质量门禁未通过，保留上一版建议并标记过期",
                     "last_published":self.latest,"simulation":{"matched":matched,"valuation":valuation}}
            self.repository.save_run(run_key,"blocked",blocked)
            if self.settings.notify_risk:
                self._notify(
                    run_key=run_key,
                    event_type="quality_blocked",
                    payload={
                        "as_of": self.snapshot.as_of.isoformat(),
                        "quality_status": q.status,
                        "quality_issue_codes": sorted(error_codes),
                        "message": "数据质量门禁未通过，日终决策未发布",
                    },
                )
            return blocked
        matching_ready=(not observation_requested or
                        bool(self.snapshot.metadata.get("simulation_matching_ready",False)))
        # Matching happens only after the quality gate. Existing positions and
        # pending exits still use full-market bars so a board-scope change
        # cannot value an excluded holding at zero.
        corporate_actions=[];risk_disposals=[]
        if matching_ready:
            last_mark=self.repository.simulation_last_trade_date()
            event_bars=[bar for bar in raw_snapshot.bars
                        if (abs(float(bar.share_multiplier or 1.0)-1.0)>1e-12 or
                            abs(float(bar.cash_dividend_per_share or 0.0))>1e-12)
                        and (last_mark is None or bar.day>date.fromisoformat(last_mark))]
            corporate_actions=self.repository.apply_corporate_actions(run_key,event_bars)
            held_before_match=self.repository.simulation_positions()
            terminal=self._terminal_simulation_symbols(raw_snapshot,raw_latest_day,held_before_match)
            risk_disposals=self.repository.write_down_terminal_positions(run_key,terminal)
            matched=self.repository.match_pending(raw_snapshot.as_of.isoformat(),ledger_bars)
        valuation=self.repository.mark_to_market(
            self.snapshot.as_of.date().isoformat(),
            {s:b.close for s,b in ledger_bars.items()},
            {"run_key":run_key,"matched":matched,"corporate_actions":corporate_actions,
             "risk_disposals":risk_disposals,"broker_connected":False,"quality":q.status,
             **simulation_lineage,"matching_ready":matching_ready},
        )
        market=assess_market(self.snapshot); themes=assess_themes(self.snapshot)
        selected_theme_names=self._weekly_theme_names(self.snapshot,themes)
        stocks=assess_stocks(
            self.snapshot,themes,self.settings.capital,
            selected_themes=set(selected_theme_names),
            max_adv_participation=self.settings.max_adv_participation,
        )
        portfolio,turnover=self._buffered_portfolio(self.snapshot,market,stocks,valuation["drawdown"])
        exit_actions=self._exit_actions(self.snapshot,stocks,turnover)
        held=self.repository.simulation_positions()
        portfolio_payload,intents,execution_state=self._simulation_execution_plan(
            portfolio,exit_actions,latest_bars,held,valuation["equity"])
        if not matching_ready:
            execution_state={**execution_state,"suppressed_intent_count":len(intents),
                             "suppressed_reason":"source_not_simulation_matching_ready"}
            intents=[]
        portfolio_risk_off=(market.exposure_cap<=0 or
                            turnover.get("exception")=="portfolio_drawdown_risk_off")
        actionable_risk=portfolio_risk_off or any(
            action.get("exit_priority") is not None and int(action["exit_priority"])<=6
            for action in exit_actions
        )
        portfolio_condition="risk_off" if portfolio_risk_off else ("partial" if len(portfolio)<3 else "healthy")
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
        backups=[item for item in stocks if item.eligible and item.symbol not in holding_symbols and
                 item.gate_results.get("theme_selection",{}).get("passed")][:3]
        history_by_symbol={}
        for bar in self.snapshot.bars:history_by_symbol.setdefault(bar.symbol,[]).append(bar)
        signal_qualified=sum(1 for item in stocks if item.eligible and
                             entry_signal(sorted(history_by_symbol[item.symbol],key=lambda bar:bar.day)) is not None)
        selection_funnel={"universe":len(stocks),"security_eligible":sum(1 for item in stocks if item.eligible),
                          "theme_entry_qualified":sum(1 for item in stocks if item.eligible and item.gate_results.get("theme_lifecycle",{}).get("passed") and item.gate_results.get("theme_selection",{}).get("passed")),
                          "signal_qualified":signal_qualified,"selected":len(portfolio),"backups":len(backups)}
        strategy_config={**config_payload,"model_version":MODEL_VERSION,"config_hash":config_hash}
        decision_id=str(uuid4()); result={"decision_id":decision_id,"run_key":run_key,
          "published":not observation_mode,"displayable":True,
          "production_published":release_mode=="production","release_mode":release_mode,
          "model_version":MODEL_VERSION,
          "config_version":self.settings_version,"config_hash":config_hash,
          "data_snapshot_hash":data_snapshot_hash,
          "strategy_config":strategy_config,
          "as_of":self.snapshot.as_of.isoformat(),"data_provenance":data_provenance,
          "provider":self.snapshot.provider,"quality":quality_view,"market":jsonable(market),"themes":jsonable(themes),
          "selected_theme_names":selected_theme_names,
          "selected_themes":[jsonable(theme) for theme in themes if theme.name in selected_theme_names],
          "portfolio":portfolio_payload,"exit_actions":exit_actions,
          "candidates":jsonable(backups),"selection_funnel":selection_funnel,
          "candidate_audit":jsonable(stocks),
          "cash_weight":round(1-sum(x.target_weight for x in portfolio),4),"portfolio_status":portfolio_status,"portfolio_reason":portfolio_reason,"model_portfolio_only":True,
          "portfolio_condition":portfolio_condition,
          "research_eligible":False if observation_mode else bool(self.snapshot.metadata.get("research_eligible",False)),
          "turnover":turnover,
          "disclaimer":"仅供研究与决策辅助，不构成收益承诺；系统不连接券商、不自动交易。"}
        audit={"id":decision_id,"timestamp":datetime.now().astimezone().isoformat(),"data_timestamp":self.snapshot.as_of.isoformat(),
               "model_version":MODEL_VERSION,"provider":self.snapshot.provider,"market_regime":market.regime,
               "config_version":self.settings_version,"config_hash":config_hash,
               "data_snapshot_hash":data_snapshot_hash,
               "input_snapshot":self.repository.input_snapshot_status(data_snapshot_hash),
               "release_mode":release_mode,"production_published":result["production_published"],
               "research_eligible":result["research_eligible"],
               "holdings":[x["symbol"] for x in portfolio_payload],"reasons":list(market.reasons),"snapshot":result}
        audit["turnover"]=turnover
        audit=jsonable(audit);self.repository.save_decision(audit,run_key)
        effective=self._next_trading_time(
            self.snapshot.as_of,
            self.snapshot.metadata.get("next_trading_day") if isinstance(self.snapshot.metadata,dict) else None,
        ).isoformat()
        if matching_ready:
            self.repository.replace_simulation_intents(run_key,self.snapshot.as_of.isoformat(),effective,intents)
        result["simulation"]={"matched":matched,"valuation":valuation,"new_intents":intents,
                              "execution_state":execution_state,"corporate_actions":corporate_actions,
                              "risk_disposals":risk_disposals,
                              "matching_ready":matching_ready,
                              "matching_reason":None if matching_ready else "公开源缺少完整涨跌停/停牌约束，仅展示决策观察，不生成或撮合模拟指令",
                              "broker_connected":False}
        self.repository.save_run(run_key,"observation" if observation_mode else "published",result)
        if self.settings.notify_eod_success:
            self._notify(
                run_key=run_key,
                event_type="eod_success",
                payload={
                    "decision_id": decision_id,
                    "as_of": self.snapshot.as_of.isoformat(),
                    "release_mode": release_mode,
                    "quality_status": quality_view.get("status"),
                    "portfolio_status": portfolio_status,
                    "portfolio_condition": portfolio_condition,
                    "market_regime": market.regime,
                    "market_exposure_cap": market.exposure_cap,
                    "message": "日终决策流水线执行成功",
                },
            )
        if self.settings.notify_risk and actionable_risk:
            self._notify(
                run_key=run_key,
                event_type="risk_alert",
                payload={
                    "decision_id": decision_id,
                    "as_of": self.snapshot.as_of.isoformat(),
                    "release_mode": release_mode,
                    "quality_status": quality_view.get("status"),
                    "portfolio_status": portfolio_status,
                    "portfolio_condition": portfolio_condition,
                    "market_regime": market.regime,
                    "market_exposure_cap": market.exposure_cap,
                    "message": ("组合级风险门禁触发，模型组合保持现金" if portfolio_risk_off else
                                "个股硬风险、趋势或止损退出条件已触发"),
                },
            )
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
        self.latest=self._runtime_freshness_view(self.latest)
        return self.latest

    def run_backtest(self, capital:float|None=None)->tuple[str,dict]:
        self.ensure(require_snapshot=True); ident=str(uuid4()); result=result_dict(run_backtest(
            self.snapshot,capital or self.settings.capital,
            max_portfolio_drawdown=self.settings.max_portfolio_drawdown,
            target_count=self.settings.target_count,
            risk_per_trade=self.settings.risk_per_trade,
            max_adv_participation=self.settings.max_adv_participation)); result["id"]=ident; result["status"]="completed"
        self.backtests[ident]=result; self.repository.save_backtest(ident,result); return ident,result

    def provider_statuses(self):
        defaults=[DeterministicDemoProvider().status(),TushareProvider().status(),AkshareProvider().status()]
        active=self.provider.status()
        return [active if item.get("provider")==self.provider.name else item for item in defaults]

    def simulation(self): return self.repository.simulation()
