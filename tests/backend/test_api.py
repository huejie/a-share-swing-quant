from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from apps.api.main import app,qualifying_simulation_weeks,service
from quant_system.engine import MODEL_VERSION

client=TestClient(app)


def test_health_and_openapi():
    assert client.get("/health").json()["automatic_trading"] is False
    assert "/api/v1/portfolio" in client.get("/openapi.json").json()["paths"]
    assert client.get("/api/v1/health/live").status_code==200
    ready=client.get("/api/v1/health/ready")
    assert ready.status_code==200 and ready.json()["checks"]["database"] is True
    assert ready.json()["checks"]["database_schema_version"] >= 2


def test_dashboard_pages_have_data_and_explanation():
    dashboard=client.get("/api/v1/dashboard").json()
    assert dashboard["model_version"]==MODEL_VERSION
    assert len(dashboard["config_hash"])==64 and len(dashboard["data_snapshot_hash"])==64
    assert len(dashboard["candidate_audit"])==dashboard["selection_funnel"]["universe"]
    assert dashboard["model_portfolio_only"] is True
    assert dashboard["portfolio_status"] in {"healthy","partial","risk_off"} and dashboard["portfolio_reason"]
    assert dashboard["market"]["reasons"]
    assert 3<=len(dashboard["selected_theme_names"])<=4
    assert {item["name"] for item in dashboard["selected_themes"]}==set(dashboard["selected_theme_names"])
    assert 3<=len(dashboard["portfolio"])<=5
    position=dashboard["portfolio"][0]
    assert position["initial_weight"]==round(position["target_weight"]*.6,4)
    assert position["expected_holding_days"]==[40,80] and position["next_review_at"]
    assert client.get("/api/v1/market").status_code==200
    assert client.get("/api/v1/themes").json()["items"]
    data_status=client.get("/api/v1/data/status").json()
    assert data_status["providers"]
    assert "provenance" in data_status


def test_stock_detail_decision_audit_and_simulation():
    d=client.get("/api/v1/dashboard").json();symbol=d["portfolio"][0]["symbol"]
    detail=client.get(f"/api/v1/stocks/{symbol}").json()
    assert detail["bars"] and detail["provenance"]["model_version"]
    decisions=client.get("/api/v1/decisions").json()["items"]
    assert decisions and client.get(f"/api/v1/decisions/{decisions[0]['id']}").status_code==200
    simulation=client.get("/api/v1/simulation").json()
    assert simulation["broker_connected"] is False and simulation["orders_sent"]==0


def test_settings_validation_and_backtest():
    assert client.get("/api/v1/settings").json()["provider"]==service.provider.name
    assert client.patch("/api/v1/settings",json={"target_count":2}).status_code==422
    assert client.patch("/api/v1/settings",json={"max_portfolio_drawdown":.181}).status_code==422
    assert client.patch("/api/v1/settings",json={"max_adv_participation":.009}).status_code==422
    assert client.patch("/api/v1/settings",json={"max_adv_participation":.021}).status_code==422
    assert client.patch("/api/v1/settings",json={"target_count":3,"capital":500_000}).status_code==200
    result=client.post("/api/v1/backtests",json={"capital":500_000})
    assert result.status_code==201
    body=result.json();assert body["result"]["assumptions"]["execution"]=="next trading-day open"
    assert client.get(f"/api/v1/backtests/{body['id']}").status_code==200


def test_historical_stale_pipeline_can_be_gated():
    response=client.post("/api/v1/pipeline/eod",json={"as_of":"2025-01-03","enforce_freshness":True}).json()
    assert response["published"] is False and response["quality"]["freshness"]=="stale"
    service.latest=None


def test_errors_share_one_contract():
    missing=client.get("/api/v1/stocks/NOPE")
    invalid=client.patch("/api/v1/settings",json={"capital":1})
    assert missing.json()["error"]["code"]=="HTTP_ERROR"
    assert invalid.json()["error"]["code"]=="VALIDATION_ERROR"
    assert client.get("/api/v1/not-a-route").json()["error"]["code"]=="HTTP_ERROR"


def test_pipeline_idempotency_does_not_duplicate_simulation_intents():
    key="pytest-idempotency-20260703"
    first=client.post("/api/v1/pipeline/eod",headers={"Idempotency-Key":key},json={"as_of":"2026-07-03"}).json()
    before=[x for x in client.get("/api/v1/simulation").json()["ledger"] if x["run_key"]==key]
    second=client.post("/api/v1/pipeline/eod",headers={"Idempotency-Key":key},json={"as_of":"2026-07-03"}).json()
    after=[x for x in client.get("/api/v1/simulation").json()["ledger"] if x["run_key"]==key]
    assert first["decision_id"]==second["decision_id"] and second["idempotent_replay"] is True
    assert len(before)==len(after)
    assert all(x["effective_at"]>x["event_time"] and x["payload"]["broker_connected"] is False for x in after)


def test_research_run_fails_closed_on_demo_data(tmp_path,monkeypatch):
    monkeypatch.setenv("QUANT_RESEARCH_PATH",str(tmp_path/"research"))
    response=client.post("/api/v1/research/runs",json={"capital":100000})
    assert response.status_code==201
    body=response.json()
    assert body["overall"]=="FAIL"
    assert body["candidate_label"]=="工程候选版/模拟观察中"
    assert client.get(f"/api/v1/research/runs/{body['id']}").status_code==200
    gates=client.get(f"/api/v1/research/runs/{body['id']}/artifacts/gates")
    assert gates.status_code==200 and gates.json()["overall"]=="FAIL"


def test_simulation_observation_requires_dense_continuous_matching_days():
    payload={"model_version":MODEL_VERSION,"provider":"licensed","matching_ready":True,"quality":"healthy",
             "config_hash":"c"*64,"settings_version":7,"data_snapshot_hash":"d"*64}
    def row(day,**changes):
        values={**payload,**changes};return {"day":day,"recorded_at":f"{day}T18:00:00+08:00",
                                             "config_hash":values["config_hash"],
                                             "settings_version":values["settings_version"],
                                             "data_snapshot_hash":values["data_snapshot_hash"],"payload":values}
    frozen_now=datetime(2026,7,16,12,tzinfo=ZoneInfo("Asia/Shanghai"))
    sparse=[row("2026-01-05"),row("2026-03-09")]
    assert qualifying_simulation_weeks(sparse,MODEL_VERSION,"licensed",now=frozen_now)<8
    assert qualifying_simulation_weeks(
        [row("2026-01-05",matching_ready=False)],MODEL_VERSION,"licensed",now=frozen_now,
    )==0

    days=[]
    current=date(2026,1,5)
    while len(days)<41:
        if current.weekday()<5:
            days.append(row(current.isoformat(),data_snapshot_hash=f"{len(days):064x}"))
        current+=timedelta(days=1)
    assert qualifying_simulation_weeks(days,MODEL_VERSION,"licensed",now=frozen_now)>=8

    # The same calendar span cannot be stitched across a settings/config change.
    split=[item if index<21 else row(item["day"],config_hash="e"*64,settings_version=8,
                                     data_snapshot_hash=item["data_snapshot_hash"])
           for index,item in enumerate(days)]
    assert qualifying_simulation_weeks(split,MODEL_VERSION,"licensed",now=frozen_now)<8
    invalid=[row("2026-07-17"),{**row("2026-01-05"),"recorded_at":"2026-01-04T18:00:00+08:00"}]
    assert qualifying_simulation_weeks(invalid,MODEL_VERSION,"licensed",now=frozen_now)==0
