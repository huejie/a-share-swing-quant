from datetime import date, datetime, timedelta
import csv
import json
import sys
from types import SimpleNamespace

import pytest

from quant_system.bundle_cli import build_bundle, validate_bundle
from quant_system.providers import CsvProvider, DeterministicDemoProvider, LicensedCsvBundleProvider, TushareProvider, provider_from_env


def write_input(root, *, authorized=True, pit=True):
    root.mkdir()
    files={
        "bars.csv":(["symbol","date","open","high","low","close","volume","amount","industry","published_at","effective_at","available_at"],[]),
        "securities.csv":(["symbol","name","listed_at","delisted_at","board"],[]),
        "theme_memberships.csv":(["symbol","theme","effective_from","effective_to","published_at","available_at"],[]),
    }
    for name,(fields,rows) in files.items():
        with (root/name).open("w",encoding="utf-8",newline="") as handle:
            writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    metadata={"batch_id":"delivery-001","provider":"fixture-vendor",
              "authorization":{"authorized":authorized,"scope":"internal-research" if authorized else ""},
              "pit":{"verified":pit,"method":"publication-time-audit" if pit else ""},
              "datasets":{"bars":{"as_of":"2026-07-06T18:00:00+08:00","required":True,"max_age_hours":72}}}
    (root/"metadata.json").write_text(json.dumps(metadata),"utf-8")
    return root


def test_provider_factory_defaults_to_demo_but_never_falls_back_on_bad_config(tmp_path):
    assert isinstance(provider_from_env({}),DeterministicDemoProvider)
    with pytest.raises(RuntimeError,match="unsupported"):
        provider_from_env({"QUANT_DATA_PROVIDER":"surprise"})
    with pytest.raises(RuntimeError,match="QUANT_DATA_PATH"):
        provider_from_env({"QUANT_DATA_PROVIDER":"csv"})
    with pytest.raises(RuntimeError,match="does not exist"):
        provider_from_env({"QUANT_DATA_PROVIDER":"csv","QUANT_DATA_PATH":str(tmp_path/"none.csv")})
    with pytest.raises(RuntimeError,match="QUANT_DATA_BUNDLE"):
        provider_from_env({"QUANT_DATA_PROVIDER":"licensed-csv"})


def test_csv_factory_uses_explicit_file(tmp_path):
    path=tmp_path/"bars.csv";path.write_text("date,symbol\n",encoding="utf-8")
    provider=provider_from_env({"QUANT_DATA_PROVIDER":"csv","QUANT_DATA_PATH":str(path)})
    assert isinstance(provider,CsvProvider) and provider.path==path


@pytest.mark.parametrize("authorized,pit,word",[(False,True,"authorization"),(True,False,"pit.verified")])
def test_bundle_builder_never_invents_authorization_or_pit(tmp_path,authorized,pit,word):
    source=write_input(tmp_path/"input",authorized=authorized,pit=pit)
    with pytest.raises(ValueError,match=word): build_bundle(source)
    metadata=json.loads((source/"metadata.json").read_text("utf-8"))
    assert metadata["authorization"]["authorized"] is authorized
    assert metadata["pit"]["verified"] is pit


def test_build_validate_and_factory_form_closed_delivery_loop(tmp_path):
    source=write_input(tmp_path/"input");target=tmp_path/"delivery"
    result=build_bundle(source,target)
    assert result["production_ready"] is True and result["manifest_hash"]
    validation=validate_bundle(target)
    assert validation["production_ready"] is True and validation["manifest_valid"] is True
    provider=provider_from_env({"QUANT_DATA_PROVIDER":"licensed-csv","QUANT_DATA_BUNDLE":str(target)})
    assert isinstance(provider,LicensedCsvBundleProvider)


def test_tampered_or_unbuilt_bundle_fails_factory_and_validator(tmp_path):
    source=write_input(tmp_path/"input")
    with pytest.raises(RuntimeError,match="not production-ready"):
        provider_from_env({"QUANT_DATA_PROVIDER":"licensed-csv","QUANT_DATA_BUNDLE":str(source)})
    build_bundle(source)
    with (source/"bars.csv").open("a",encoding="utf-8") as handle:handle.write("tampered\n")
    with pytest.raises(ValueError):validate_bundle(source)
    with pytest.raises(RuntimeError,match="not production-ready"):
        provider_from_env({"QUANT_DATA_PROVIDER":"licensed-csv","QUANT_DATA_BUNDLE":str(source)})


class FakeFrame:
    def __init__(self, records): self.records = records
    def to_dict(self, orient="records"):
        assert orient == "records"
        return self.records


class FakeTusharePro:
    def __init__(self, sessions):
        self.sessions = sessions
        self.daily_requests = []

    def trade_cal(self, **kwargs):
        return FakeFrame([{"cal_date": day.strftime("%Y%m%d"), "is_open": 1} for day in self.sessions])

    def stock_basic(self, **kwargs):
        return FakeFrame([{"ts_code": "000001.SZ", "name": "平安银行", "market": "主板", "list_date": "19910403"}])

    def daily(self, **kwargs):
        requested = kwargs["trade_date"]
        self.daily_requests.append(requested)
        return FakeFrame([{"ts_code": "000001.SZ", "trade_date": requested, "open": 10, "high": 11,
                           "low": 9, "close": 10.5, "vol": 12.5, "amount": 131.25}])


def install_fake_tushare(monkeypatch, pro):
    module = SimpleNamespace(pro_api=lambda token: pro if token == "mock-token" else (_ for _ in ()).throw(ValueError("bad token")))
    monkeypatch.setitem(sys.modules, "tushare", module)


def test_tushare_fetches_120_sessions_with_basic_names_and_marks_snapshot_non_pit(monkeypatch):
    end = date(2026, 7, 3)
    sessions = [end - timedelta(days=offset) for offset in range(190) if (end - timedelta(days=offset)).weekday() < 5]
    sessions = sorted(sessions)
    pro = FakeTusharePro(sessions)
    install_fake_tushare(monkeypatch, pro)

    provider = provider_from_env({"QUANT_DATA_PROVIDER": "tushare", "QUANT_TUSHARE_TOKEN": "mock-token",
                                  "QUANT_TUSHARE_MIN_REQUEST_INTERVAL_SECONDS": "0"})
    assert isinstance(provider, TushareProvider)
    snapshot = provider.load(end)

    assert len(snapshot.bars) == 120
    assert {bar.name for bar in snapshot.bars} == {"平安银行"}
    assert all(bar.volume == 1250 and bar.amount == 131250 for bar in snapshot.bars)
    assert len(pro.daily_requests) == 120
    assert snapshot.metadata["trading_day_count"] == 120
    assert snapshot.metadata["production_ready"] is False
    assert snapshot.metadata["pit_verified"] is False
    assert snapshot.metadata["research_eligible"] is False


def test_tushare_rejects_non_trading_as_of_without_daily_request(monkeypatch):
    end = date(2026, 7, 5)  # Sunday
    sessions = [date(2026, 1, 1) + timedelta(days=offset) for offset in range(190)
                if (date(2026, 1, 1) + timedelta(days=offset)).weekday() < 5 and date(2026, 1, 1) + timedelta(days=offset) < end]
    pro = FakeTusharePro(sessions)
    install_fake_tushare(monkeypatch, pro)

    with pytest.raises(ValueError, match="not an SSE trading day"):
        TushareProvider("mock-token").load(end)
    assert pro.daily_requests == []


def test_tushare_factory_requires_explicit_token():
    with pytest.raises(RuntimeError, match="QUANT_TUSHARE_TOKEN"):
        provider_from_env({"QUANT_DATA_PROVIDER": "tushare"})
