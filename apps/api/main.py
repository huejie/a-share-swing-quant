from __future__ import annotations
from datetime import date,datetime
import os
import secrets
from typing import Any
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field
from quant_system.models import jsonable
from quant_system.notifications import NotificationDispatcher
from quant_system.engine import MODEL_VERSION
from quant_system.research_service import read_research_artifact, read_research_run, run_research_package
from quant_system.service import QuantService
from quant_system.providers import provider_from_env

app=FastAPI(title="A股中期波段精选系统 API",version="0.4.0",description="研究、解释、回测与模拟组合；不包含自动交易。")
service=QuantService(provider=provider_from_env())
admin_key_header=APIKeyHeader(name="X-Admin-Key",auto_error=False,description="个人管理密钥；仅用于设置、日终和研究写操作")

def require_admin(key:str|None=Security(admin_key_header)):
    configured=os.getenv("QUANT_ADMIN_API_KEY","").strip()
    # Local development remains frictionless when no key is configured.  The
    # deployment profile always sets a key and therefore fails closed.
    if configured and (not key or not secrets.compare_digest(key,configured)):
        raise HTTPException(401,"需要有效的个人管理密钥",headers={"WWW-Authenticate":"ApiKey"})
    return True

def error_body(request:Request,code:str,message:str,details=None):
    return {"error":{"code":code,"message":message,"details":details or [],"path":request.url.path}}

@app.exception_handler(HTTPException)
async def http_error(request:Request,exc:HTTPException):
    return JSONResponse(error_body(request,"HTTP_ERROR",str(exc.detail)),status_code=exc.status_code)

@app.exception_handler(StarletteHTTPException)
async def starlette_http_error(request:Request,exc:StarletteHTTPException):
    return JSONResponse(error_body(request,"HTTP_ERROR",str(exc.detail)),status_code=exc.status_code)

@app.exception_handler(RequestValidationError)
async def validation_error(request:Request,exc:RequestValidationError):
    return JSONResponse(error_body(request,"VALIDATION_ERROR","请求参数不符合接口约束",exc.errors()),status_code=422)

@app.exception_handler(Exception)
async def unexpected_error(request:Request,exc:Exception):
    return JSONResponse(error_body(request,"INTERNAL_ERROR","服务暂时无法完成请求"),status_code=500)

class SettingsPatch(BaseModel):
    capital: float|None=Field(None,ge=100_000,le=10_000_000)
    target_count: int|None=Field(None,ge=3,le=5)
    max_portfolio_drawdown: float|None=Field(None,ge=.05,le=.18)
    risk_per_trade: float|None=Field(None,ge=.005,le=.02)
    max_adv_participation: float|None=Field(None,ge=.01,le=.02)
    include_main: bool|None=None
    include_chinext: bool|None=None
    include_star: bool|None=None
    include_bse: bool|None=None
    notify_eod_success: bool|None=None
    notify_risk: bool|None=None
    notification_channel: str|None=Field(None,pattern="^(none|email|webhook)$")

class PipelineRequest(BaseModel):
    as_of: date|None=None
    enforce_freshness: bool=False
    run_key: str|None=Field(None,min_length=6,max_length=128)

class BacktestRequest(BaseModel):
    capital: float|None=Field(None,ge=100_000,le=10_000_000)

class ResearchRequest(BaseModel):
    capital: int=Field(1_000_000,ge=100_000,le=10_000_000)

class ProductResponse(BaseModel):
    model_config=ConfigDict(extra="allow")

class DashboardResponse(ProductResponse):
    as_of: str|None=None
    model_version: str
    config_version: int=0
    config_hash: str=""
    data_snapshot_hash: str=""
    strategy_config: dict[str,Any]=Field(default_factory=dict)
    provider: str|None=None
    market: dict[str,Any]=Field(default_factory=dict)
    portfolio: list[dict[str,Any]]=Field(default_factory=list)
    exit_actions: list[dict[str,Any]]=Field(default_factory=list)
    candidates: list[dict[str,Any]]=Field(default_factory=list)
    candidate_audit: list[dict[str,Any]]=Field(default_factory=list)
    selected_theme_names: list[str]=Field(default_factory=list)
    selected_themes: list[dict[str,Any]]=Field(default_factory=list)
    cash_weight: float=1.0

class PortfolioResponse(ProductResponse):
    positions: list[dict[str,Any]]=Field(default_factory=list)
    cash_weight: float=1.0
    model_portfolio_only: bool=True

class DataStatusResponse(ProductResponse):
    quality: dict[str,Any]=Field(default_factory=dict)
    providers: list[dict[str,Any]]=Field(default_factory=list)
    active: str=""
    provenance: dict[str,Any]=Field(default_factory=dict)

class SimulationResponse(ProductResponse):
    simulated_account: dict[str,Any]|None=None
    simulated_positions: list[dict[str,Any]]=Field(default_factory=list)
    ledger: list[dict[str,Any]]=Field(default_factory=list)
    daily_equity: list[dict[str,Any]]=Field(default_factory=list)

class SettingsResponse(ProductResponse):
    provider: str
    capital: float
    target_count: int
    max_portfolio_drawdown: float
    risk_per_trade: float
    max_adv_participation: float=.02
    include_main: bool
    include_chinext: bool
    include_star: bool
    include_bse: bool
    notify_eod_success: bool=True
    notify_risk: bool=True
    notification_channel: str="none"
    automatic_trading: bool=False

def qualifying_simulation_weeks(rows:list[dict[str,Any]],model_version:str,provider:str,
                                *,now:datetime|None=None)->float:
    """Count the best continuous segment from one complete immutable lineage."""
    now=now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:raise ValueError("now must be timezone-aware")
    groups:dict[tuple[str,str,str,int],set[date]]={}
    for row in rows:
        payload=row.get("payload") if isinstance(row,dict) else None
        if not isinstance(payload,dict): continue
        if (payload.get("model_version")!=model_version or payload.get("provider")!=provider or
                payload.get("matching_ready") is not True or payload.get("quality")=="blocked"):
            continue
        config_hash=str(row.get("config_hash") or payload.get("config_hash") or "")
        snapshot_hash=str(row.get("data_snapshot_hash") or payload.get("data_snapshot_hash") or "")
        settings_version=row.get("settings_version",payload.get("settings_version"))
        if len(config_hash)!=64 or len(snapshot_hash)!=64:
            continue
        try:
            settings_version=int(settings_version)
            trade_day=date.fromisoformat(str(row.get("day") or row.get("trade_date")))
            recorded=datetime.fromisoformat(str(row.get("recorded_at")))
        except (TypeError,ValueError):
            continue
        if recorded.tzinfo is None:
            continue
        normalized=recorded.astimezone(now.tzinfo)
        if trade_day>now.date() or normalized>now or normalized.date()<trade_day:
            continue
        lineage=(model_version,provider,config_hash,settings_version)
        groups.setdefault(lineage,set()).add(trade_day)
    best=0.0
    for days in groups.values():
        ordered=sorted(days)
        if not ordered:continue
        segments=[[ordered[0]]]
        for day in ordered[1:]:
            if (day-segments[-1][-1]).days>14:
                segments.append([])
            segments[-1].append(day)
        for segment in segments:
            span=(segment[-1]-segment[0]).days/7 if len(segment)>1 else 0.0
            trading_weeks=len(segment)/5
            best=max(best,min(span,trading_weeks))
    return round(best,2)

@app.get("/health",response_model=dict[str,Any])
def health(): return {"status":"ok","service":"quant-api","automatic_trading":False}

@app.get("/api/v1/health/live",response_model=dict[str,Any])
def live(): return {"status":"alive","service":"quant-api"}

@app.get("/api/v1/health/ready",response_model=dict[str,Any])
def ready():
    database=service.repository.ping(); active=service.provider.status(); provider=bool(active.get("available",False))
    latest=service.ensure() or {}; freshness=latest.get("quality",{}).get("freshness","unknown")
    quality_status=latest.get("quality",{}).get("status","unknown")
    release_mode=latest.get("release_mode","demo" if service.provider.name=="deterministic-demo" else "production")
    quality_usable=(quality_status!="blocked" or release_mode=="observation_only" or
                    service.provider.name=="deterministic-demo")
    ready_state=database and provider and quality_usable and (freshness=="fresh" or service.provider.name=="deterministic-demo")
    body={"status":"ready" if ready_state else "degraded","checks":{"database":database,"database_schema_version":service.repository.schema_version(),"provider":active,"freshness":freshness,
          "quality_status":quality_status,"release_mode":release_mode,
          "last_successful_eod":latest.get("as_of") if latest.get("published") or latest.get("displayable") else None},"automatic_trading":False}
    return JSONResponse(body,status_code=200 if ready_state else 503)

@app.get("/api/v1/dashboard",response_model=DashboardResponse)
def dashboard(): return service.ensure()

@app.get("/api/v1/market",response_model=dict[str,Any])
def market():
    d=service.ensure(); return {"as_of":d["as_of"],"quality":d["quality"],"market":d["market"]}

@app.get("/api/v1/themes",response_model=dict[str,Any])
def themes(): return {"items":service.ensure()["themes"],"as_of":service.ensure()["as_of"]}

@app.get("/api/v1/portfolio",response_model=PortfolioResponse)
def portfolio():
    d=service.ensure(); return {"positions":d["portfolio"],"cash_weight":d["cash_weight"],"model_portfolio_only":True,"disclaimer":d["disclaimer"]}

@app.get("/api/v1/candidates",response_model=dict[str,Any])
def candidates(): return {"items":service.ensure()["candidates"]}

@app.get("/api/v1/stocks/{symbol}",response_model=dict[str,Any])
def stock(symbol:str):
    d=service.ensure(require_snapshot=True); all_items=d["portfolio"]+d["candidates"]
    item=next((x for x in all_items if x["symbol"].upper()==symbol.upper()),None)
    if not item: raise HTTPException(404,"股票不在当前精选或备选池")
    bars=[jsonable(b) for b in service.snapshot.bars if b.symbol.upper()==symbol.upper()]
    return {"stock":item,"bars":bars[-90:],"provenance":{"provider":d["provider"],"data_timestamp":d["as_of"],"model_version":MODEL_VERSION}}

@app.get("/api/v1/decisions",response_model=dict[str,Any])
def decisions(limit:int=Query(20,ge=1,le=100)): return {"items":[{k:v for k,v in x.items() if k!="snapshot"} for x in service.repository.list_decisions(limit)]}

@app.get("/api/v1/decisions/{decision_id}",response_model=dict[str,Any])
def decision(decision_id:str):
    x=service.repository.get_decision(decision_id)
    if not x: raise HTTPException(404,"决策记录不存在")
    snapshot_hash=x.get("data_snapshot_hash") or x.get("snapshot",{}).get("data_snapshot_hash")
    return {**x,"input_snapshot":service.repository.input_snapshot_status(snapshot_hash) if snapshot_hash else {"available":False}}

@app.get("/api/v1/data/status",response_model=DataStatusResponse)
def data_status():
    d=service.ensure(); return {"quality":d["quality"],"providers":service.provider_statuses(),"active":d["provider"],
                                "provenance":d.get("data_provenance",{})}

@app.post("/api/v1/pipeline/eod",response_model=dict[str,Any],dependencies=[Security(require_admin)])
def pipeline(req:PipelineRequest,idempotency_key:str|None=Header(None,alias="Idempotency-Key"),x_idempotency_key:str|None=Header(None,alias="X-Idempotency-Key")):
    return service.run_eod(req.as_of,enforce_freshness=req.enforce_freshness,run_key=idempotency_key or x_idempotency_key or req.run_key)

@app.post("/api/v1/backtests",status_code=201,response_model=dict[str,Any],dependencies=[Security(require_admin)])
def create_backtest(req:BacktestRequest):
    ident,result=service.run_backtest(req.capital); return {"id":ident,"status":result["status"],"result":result}

@app.get("/api/v1/backtests",response_model=dict[str,Any])
def list_backtests(): return {"items":service.repository.list_backtests()}

@app.get("/api/v1/backtests/{backtest_id}",response_model=dict[str,Any])
def backtest(backtest_id:str):
    result=service.repository.get_backtest(backtest_id)
    if result is None:raise HTTPException(404,"回测不存在")
    return result

@app.post("/api/v1/research/runs",status_code=201,response_model=dict[str,Any],dependencies=[Security(require_admin)])
def create_research_run(req:ResearchRequest):
    service.ensure(require_snapshot=True)
    simulation=service.simulation().get("daily_equity",[])
    weeks=qualifying_simulation_weeks(simulation,MODEL_VERSION,service.provider.name)
    return run_research_package(service.snapshot,os.getenv("QUANT_RESEARCH_PATH","data/research"),capital=req.capital,simulation_weeks=weeks)

@app.get("/api/v1/research/runs/{run_id}",response_model=dict[str,Any])
def research_run(run_id:str):
    result=read_research_run(run_id,os.getenv("QUANT_RESEARCH_PATH","data/research"))
    if result is None: raise HTTPException(404,"研究运行不存在")
    return result

@app.get("/api/v1/research/runs/{run_id}/artifacts/{artifact}",response_model=dict[str,Any])
def research_artifact(run_id:str,artifact:str):
    filename=artifact if artifact.endswith(".json") else f"{artifact}.json"
    result=read_research_artifact(run_id,filename,os.getenv("QUANT_RESEARCH_PATH","data/research"))
    if result is None: raise HTTPException(404,"研究制品不存在")
    return result

@app.get("/api/v1/simulation",response_model=SimulationResponse)
def simulation():
    d=service.ensure(); ledger=service.simulation(); return {"status":"active","name":"MVP 模型组合","started_at":d["as_of"],"positions":d["portfolio"],
       "cash_weight":d["cash_weight"],"orders_sent":0,"broker_connected":False,"simulated_account":ledger["account"],"simulated_positions":ledger["positions"],"ledger":ledger["ledger"],"daily_equity":ledger["daily_equity"],
       "note":"仅记录下一交易日模型意图和模拟台账，不触达券商。"}

@app.get("/api/v1/settings",response_model=SettingsResponse)
def get_settings(): return jsonable(service.settings)

@app.patch("/api/v1/settings",response_model=SettingsResponse,dependencies=[Security(require_admin)])
def patch_settings(patch:SettingsPatch):
    try:return service.update_settings(patch.model_dump(exclude_none=True))
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc

@app.get("/api/v1/settings/audit",response_model=dict[str,Any],dependencies=[Security(require_admin)])
def settings_audit(limit:int=Query(20,ge=1,le=100)):
    return {"items":service.repository.list_settings_audit(limit=limit)}

@app.get("/api/v1/notifications",response_model=dict[str,Any],dependencies=[Security(require_admin)])
def notifications(limit:int=Query(50,ge=1,le=200),status:str|None=Query(None,pattern="^(pending|sending|sent|failed|skipped)$"),
                  event_type:str|None=None):
    return {"items":service.repository.list_notifications(limit,status=status,event_type=event_type)}

@app.post("/api/v1/notifications/{notification_id}/retry",response_model=dict[str,Any],dependencies=[Security(require_admin)])
def retry_notification(notification_id:str):
    try:
        item=NotificationDispatcher(service.repository).retry(notification_id)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    if item is None:
        raise HTTPException(404,"通知记录不存在")
    return item
