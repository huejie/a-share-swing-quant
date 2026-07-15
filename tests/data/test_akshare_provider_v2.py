from __future__ import annotations

from datetime import date, timedelta
import sys
from types import SimpleNamespace

import pytest

from quant_system.providers import AkshareProvider
from quant_system.quality import check_quality


class Frame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient="records"):
        assert orient == "records"
        return self.records


class FakeAkshare:
    def __init__(self, end: date, *, missing_industry: bool = False, invalid_ohlc: bool = False,
                 metadata_failure: bool = False):
        self.end = end
        self.missing_industry = missing_industry
        self.invalid_ohlc = invalid_ohlc
        self.metadata_failure = metadata_failure
        self.history_calls: list[dict] = []

    def stock_individual_info_em(self, **kwargs):
        if self.metadata_failure:
            raise ConnectionError("upstream closed connection")
        assert kwargs["symbol"] == "000001"
        return Frame([
            {"item": "股票简称", "value": "平安银行"},
            {"item": "行业", "value": None if self.missing_industry else "银行"},
            {"item": "上市时间", "value": 19910403},
        ])

    def stock_zh_a_hist(self, **kwargs):
        self.history_calls.append(kwargs)
        records = []
        for offset in range(65):
            day = self.end - timedelta(days=64 - offset)
            op, close, high, low = 10.0, 10.2, 10.5, 9.8
            if self.invalid_ohlc and offset == 20:
                high = 9.0
            records.append({"日期": day.isoformat(), "股票代码": "000001", "开盘": op,
                            "收盘": close, "最高": high, "最低": low,
                            "成交量": 1_000_000, "成交额": 10_200_000})
        return Frame(records)


def install(monkeypatch, fake: FakeAkshare) -> None:
    module = SimpleNamespace(stock_individual_info_em=fake.stock_individual_info_em,
                             stock_zh_a_hist=fake.stock_zh_a_hist)
    monkeypatch.setitem(sys.modules, "akshare", module)


def test_akshare_qfq_observation_snapshot_has_strict_enrichment_contract(monkeypatch):
    end = date.today()
    fake = FakeAkshare(end)
    install(monkeypatch, fake)

    snapshot = AkshareProvider(("000001.SZ",)).load(end)

    assert len(snapshot.bars) == 65
    assert all(bar.name == "平安银行" for bar in snapshot.bars)
    assert all(bar.industry == "银行" and bar.theme == "银行" for bar in snapshot.bars)
    assert all(bar.adj_factor == 1.0 and bar.quality == 50 and bar.catalyst == 50 for bar in snapshot.bars)
    assert fake.history_calls[0]["adjust"] == "qfq"
    assert fake.history_calls[0]["period"] == "daily"
    assert snapshot.metadata["observation_only"] is True
    assert snapshot.metadata["public_data"] is True
    assert snapshot.metadata["data_quality"] == {
        "status": "observation_ready", "qfq_validated": True,
        "industry_complete": True, "history_counts": {"000001.SZ": 65},
    }
    assert snapshot.metadata["theme_mapping"]["status"] == "industry_fallback"
    assert snapshot.metadata["enrichments"]["adj_factor"]["status"] == "available"
    assert snapshot.metadata["enrichments"]["adj_factor"]["method"] == "provider_qfq"
    assert snapshot.metadata["enrichments"]["daily_basic"]["status"] == "unavailable"
    assert snapshot.metadata["enrichments"]["index_daily"]["status"] == "unavailable"
    assert snapshot.metadata["production_ready"] is False
    assert snapshot.metadata["pit_verified"] is False
    assert snapshot.metadata["research_eligible"] is False

    report = check_quality(snapshot)
    error_codes = {issue.code for issue in report.issues if issue.severity == "error"}
    warning_codes = {issue.code for issue in report.issues if issue.severity == "warning"}
    assert error_codes == {"NOT_PRODUCTION_READY"}
    assert warning_codes == {"ENRICHMENT_DEGRADED:daily_basic", "ENRICHMENT_DEGRADED:index_daily"}


def test_akshare_missing_industry_blocks_before_history_is_used(monkeypatch):
    fake = FakeAkshare(date.today(), missing_industry=True)
    install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="industry|行业"):
        AkshareProvider(("000001.SZ",)).load(date.today())
    assert fake.history_calls == []


def test_akshare_invalid_qfq_ohlc_blocks_observation(monkeypatch):
    fake = FakeAkshare(date.today(), invalid_ohlc=True)
    install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="invalid OHLCV.*observation is blocked"):
        AkshareProvider(("000001.SZ",)).load(date.today())
    assert fake.history_calls[0]["adjust"] == "qfq"


def test_akshare_explicit_metadata_fallback_is_audited_when_live_metadata_fails(monkeypatch, tmp_path):
    end = date.today()
    fake = FakeAkshare(end, metadata_failure=True)
    install(monkeypatch, fake)
    metadata = tmp_path / "universe.csv"
    metadata.write_text(
        "symbol,name,industry,list_date\n000001.SZ,平安银行,银行,1991-04-03\n",
        encoding="utf-8",
    )

    snapshot = AkshareProvider(("000001.SZ",), metadata_path=metadata).load(end)

    assert {bar.name for bar in snapshot.bars} == {"平安银行"}
    assert {bar.industry for bar in snapshot.bars} == {"银行"}
    audit = snapshot.metadata["security_metadata"]
    assert audit["live_endpoint_available"] is False
    assert audit["live_endpoint_error"] == "AKShare stock_individual_info_em request failed"
    assert audit["sources"] == {"000001.SZ": "configured_static_fallback"}
    assert snapshot.metadata["theme_mapping"]["status"] == "industry_fallback"


def test_akshare_metadata_fallback_still_blocks_if_requested_symbol_is_missing(monkeypatch, tmp_path):
    fake = FakeAkshare(date.today(), metadata_failure=True)
    install(monkeypatch, fake)
    metadata = tmp_path / "universe.csv"
    metadata.write_text(
        "symbol,name,industry,list_date\n600519.SH,贵州茅台,白酒,2001-08-27\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="configured metadata is missing.*observation is blocked"):
        AkshareProvider(("000001.SZ",), metadata_path=metadata).load(date.today())
    assert fake.history_calls == []


def test_akshare_factory_interval_validation_is_fail_closed(monkeypatch):
    install(monkeypatch, FakeAkshare(date.today()))
    from quant_system.providers import provider_from_env

    with pytest.raises(RuntimeError, match="non-negative number"):
        provider_from_env({"QUANT_DATA_PROVIDER": "akshare", "QUANT_AKSHARE_SYMBOLS": "000001.SZ",
                           "QUANT_AKSHARE_MIN_REQUEST_INTERVAL_SECONDS": "bad"})
    provider = provider_from_env({"QUANT_DATA_PROVIDER": "akshare", "QUANT_AKSHARE_SYMBOLS": "000001.SZ",
                                  "QUANT_AKSHARE_MIN_REQUEST_INTERVAL_SECONDS": "0.75"})
    assert isinstance(provider, AkshareProvider)
    assert provider.min_request_interval_seconds == 0.75
