from __future__ import annotations
from datetime import date
import os
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field
from quant_system.models import jsonable
from quant_system.engine import MODEL_VERSION
from quant_system.research_service import read_research_artifact, read_research_run, run_research_package
from quant_system.service import QuantService
from quant_system.providers import provider_from_env

app=FastAPI(title="A股中期波段精选系统 API",version="0.1.0",description="研究、解释、回测与模拟组合；不包含自动交易。")
service=QuantService(provider=provider_from_env())

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
    max_portfolio_drawdown: float|None=Field(None,ge=.05,le=.30)
    risk_per_trade: float|None=Field(None,ge=.005,le=.02)
    include_main: bool|None=None
    include_chinext: bool|None=None
    include_star: bool|None=None
    include_bse: bool|None=None

class PipelineRequest(BaseModel):
    as_of: date|None=None
    enforce_freshness: bool=False
    run_key: str|None=Field(None,min_length=6,max_length=128)

class BacktestRequest(BaseModel):
    capital: float|None=Field(None,ge=100_000,le=10_000_000)

class ResearchRequest(BaseModel):
    capital: int=Field(1_000_000,ge=100_000,le=10_000_000)

@app.get("/health")
def health(): return {"status":"ok","service":"quant-api","automatic_trading":False}

@app.get("/api/v1/health/live")
def live(): return {"status":"alive","service":"quant-api"}

@app.get("/api/v1/health/ready")
def ready():
    database=service.repository.ping(); active=service.provider.status(); provider=bool(active.get("available",False))
    latest=service.ensure() or {}; freshness=latest.get("quality",{}).get("freshness","unknown")
    quality_status=latest.get("quality",{}).get("status","unknown")
    release_mode=latest.get("release_mode","demo" if service.provider.name=="deterministic-demo" else "production")
    quality_usable=(quality_status!="blocked" or release_mode=="observation_only" or
                    service.provider.name=="deterministic-demo")
    ready_state=database and provider and quality_usable and (freshness=="fresh" or service.provider.name=="deterministic-demo")
    body={"status":"ready" if ready_state else "degraded","checks":{"database":database,"provider":active,"freshness":freshness,
          "quality_status":quality_status,"release_mode":release_mode,
          "last_successful_eod":latest.get("as_of") if latest.get("published") or latest.get("displayable") else None},"automatic_trading":False}
    return JSONResponse(body,status_code=200 if ready_state else 503)

@app.get("/api/v1/dashboard")
def dashboard(): return service.ensure()

@app.get("/api/v1/market")
def market():
    d=service.ensure(); return {"as_of":d["as_of"],"quality":d["quality"],"market":d["market"]}

@app.get("/api/v1/themes")
def themes(): return {"items":service.ensure()["themes"],"as_of":service.ensure()["as_of"]}

@app.get("/api/v1/portfolio")
def portfolio():
    d=service.ensure(); return {"positions":d["portfolio"],"cash_weight":d["cash_weight"],"model_portfolio_only":True,"disclaimer":d["disclaimer"]}

@app.get("/api/v1/candidates")
def candidates(): return {"items":service.ensure()["candidates"]}

@app.get("/api/v1/stocks/{symbol}")
def stock(symbol:str):
    d=service.ensure(require_snapshot=True); all_items=d["portfolio"]+d["candidates"]
    item=next((x for x in all_items if x["symbol"].upper()==symbol.upper()),None)
    if not item: raise HTTPException(404,"股票不在当前精选或备选池")
    bars=[jsonable(b) for b in service.snapshot.bars if b.symbol.upper()==symbol.upper()]
    return {"stock":item,"bars":bars[-90:],"provenance":{"provider":d["provider"],"data_timestamp":d["as_of"],"model_version":MODEL_VERSION}}

@app.get("/api/v1/decisions")
def decisions(limit:int=Query(20,ge=1,le=100)): return {"items":[{k:v for k,v in x.items() if k!="snapshot"} for x in service.repository.list_decisions(limit)]}

@app.get("/api/v1/decisions/{decision_id}")
def decision(decision_id:str):
    x=service.repository.get_decision(decision_id)
    if not x: raise HTTPException(404,"决策记录不存在")
    return x

@app.get("/api/v1/data/status")
def data_status():
    d=service.ensure(); return {"quality":d["quality"],"providers":service.provider_statuses(),"active":d["provider"],
                                "provenance":d.get("data_provenance",{})}

@app.post("/api/v1/pipeline/eod")
def pipeline(req:PipelineRequest,idempotency_key:str|None=Header(None,alias="Idempotency-Key"),x_idempotency_key:str|None=Header(None,alias="X-Idempotency-Key")):
    return service.run_eod(req.as_of,enforce_freshness=req.enforce_freshness,run_key=idempotency_key or x_idempotency_key or req.run_key)

@app.post("/api/v1/backtests",status_code=201)
def create_backtest(req:BacktestRequest):
    ident,result=service.run_backtest(req.capital); return {"id":ident,"status":result["status"],"result":result}

@app.get("/api/v1/backtests")
def list_backtests(): return {"items":service.repository.list_backtests()}

@app.get("/api/v1/backtests/{backtest_id}")
def backtest(backtest_id:str):
    result=service.repository.get_backtest(backtest_id)
    if result is None:raise HTTPException(404,"回测不存在")
    return result

@app.post("/api/v1/research/runs",status_code=201)
def create_research_run(req:ResearchRequest):
    service.ensure(require_snapshot=True)
    simulation=service.simulation().get("daily_equity",[])
    weeks=0.0
    if len(simulation)>1:
        weeks=max(0.0,(date.fromisoformat(simulation[-1]["day"])-date.fromisoformat(simulation[0]["day"])).days/7)
    return run_research_package(service.snapshot,os.getenv("QUANT_RESEARCH_PATH","data/research"),capital=req.capital,simulation_weeks=weeks)

@app.get("/api/v1/research/runs/{run_id}")
def research_run(run_id:str):
    result=read_research_run(run_id,os.getenv("QUANT_RESEARCH_PATH","data/research"))
    if result is None: raise HTTPException(404,"研究运行不存在")
    return result

@app.get("/api/v1/research/runs/{run_id}/artifacts/{artifact}")
def research_artifact(run_id:str,artifact:str):
    filename=artifact if artifact.endswith(".json") else f"{artifact}.json"
    result=read_research_artifact(run_id,filename,os.getenv("QUANT_RESEARCH_PATH","data/research"))
    if result is None: raise HTTPException(404,"研究制品不存在")
    return result

@app.get("/api/v1/simulation")
def simulation():
    d=service.ensure(); ledger=service.simulation(); return {"status":"active","name":"MVP 模型组合","started_at":d["as_of"],"positions":d["portfolio"],
       "cash_weight":d["cash_weight"],"orders_sent":0,"broker_connected":False,"simulated_account":ledger["account"],"simulated_positions":ledger["positions"],"ledger":ledger["ledger"],"daily_equity":ledger["daily_equity"],
       "note":"仅记录下一交易日模型意图和模拟台账，不触达券商。"}

@app.get("/api/v1/settings")
def get_settings(): return jsonable(service.settings)

@app.patch("/api/v1/settings")
def patch_settings(patch:SettingsPatch):
    for key,value in patch.model_dump(exclude_none=True).items():setattr(service.settings,key,value)
    service.latest=None
    return jsonable(service.settings)
