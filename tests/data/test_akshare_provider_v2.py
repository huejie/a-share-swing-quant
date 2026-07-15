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
                 metadata_failure: bool = False, history_failure: bool = False):
        self.end = end
        self.missing_industry = missing_industry
        self.invalid_ohlc = invalid_ohlc
        self.metadata_failure = metadata_failure
        self.history_failure = history_failure
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
        if self.history_failure:
            raise ConnectionError("eastmoney history unavailable")
        return self._history_records()

    def stock_zh_a_daily(self, **kwargs):
        self.history_calls.append(kwargs)
        records = self._history_records()
        return Frame([{"date": row["日期"], "open": row["开盘"], "close": row["收盘"],
                       "high": row["最高"], "low": row["最低"],
                       "volume": row["成交量"], "amount": row["成交额"]} for row in records.records])

    def _history_records(self):
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
                             stock_zh_a_hist=fake.stock_zh_a_hist,
                             stock_zh_a_daily=fake.stock_zh_a_daily)
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
    assert all(bar.volume == 100_000_000 for bar in snapshot.bars)
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


def test_akshare_sina_qfq_fallback_is_audited_and_preserves_share_volume(monkeypatch):
    end = date.today()
    fake = FakeAkshare(end, history_failure=True)
    install(monkeypatch, fake)

    snapshot = AkshareProvider(("000001.SZ",)).load(end)

    assert len(snapshot.bars) == 65
    assert all(bar.volume == 1_000_000 for bar in snapshot.bars)
    assert fake.history_calls[0]["symbol"] == "000001"
    assert fake.history_calls[1]["symbol"] == "sz000001"
    assert snapshot.metadata["price_history"]["sources"] == {"000001.SZ": "sina_qfq_fallback"}
    assert snapshot.metadata["price_history"]["eastmoney_endpoint_available"] is False
    assert snapshot.metadata["price_history"]["volume_unit"] == "shares"


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


def test_dynamic_snapshot_screen_is_bounded_deterministic_and_excludes_unsupported_names():
    records = [
        {"代码": "600000", "名称": "浦发银行", "最新价": 12, "成交额": 500_000_000, "60日涨跌幅": -10},
        {"代码": "688001", "名称": "华兴源创", "最新价": 30, "成交额": 300_000_000, "60日涨跌幅": 20},
        {"代码": "000001", "名称": "平安银行", "最新价": 10, "成交额": 200_000_000, "60日涨跌幅": 10},
        {"代码": "300001", "名称": "特锐德", "最新价": 20, "成交额": 150_000_000, "60日涨跌幅": 100},
        {"代码": "830001", "名称": "北交样本", "最新价": 8, "成交额": 900_000_000, "60日涨跌幅": 90},
        {"代码": "200001", "名称": "深B样本", "最新价": 5, "成交额": 900_000_000, "60日涨跌幅": 90},
        {"代码": "000002", "名称": "ST样本", "最新价": 5, "成交额": 900_000_000, "60日涨跌幅": 90},
        {"代码": "000003", "名称": "低成交", "最新价": 5, "成交额": 99_999_999, "60日涨跌幅": 90},
        {"代码": "000004", "名称": "停牌价", "最新价": None, "成交额": 900_000_000, "60日涨跌幅": 90},
    ]
    api = SimpleNamespace(stock_zh_a_spot_em=lambda: Frame(records))
    provider = AkshareProvider(("000001.SZ",), dynamic_universe_limit=3)

    symbols, audit = provider._select_observation_universe(api, date.today(), allow_current_snapshot=True)

    assert symbols == ("600000.SH", "688001.SH", "000001.SZ")
    assert audit["mode"] == "dynamic_current_snapshot"
    assert audit["selected_count"] == 3
    assert audit["rejected"] == {
        "invalid_or_unsupported_code": 2,
        "duplicate_symbol": 0,
        "st_or_delisting_name": 1,
        "invalid_price": 1,
        "below_min_turnover": 1,
    }
    assert audit["selection_snapshot_is_pit"] is False
    assert audit["simulation_matching_ready"] is False


def test_dynamic_industry_failure_falls_back_to_complete_configured_pool_before_history(monkeypatch, tmp_path):
    end = date.today()
    fake = FakeAkshare(end, metadata_failure=True)
    module = SimpleNamespace(
        stock_zh_a_spot_em=lambda: Frame([
            {"代码": "600000", "名称": "浦发银行", "最新价": 12,
             "成交额": 500_000_000, "60日涨跌幅": 15},
        ]),
        stock_individual_info_em=fake.stock_individual_info_em,
        stock_zh_a_hist=fake.stock_zh_a_hist,
        stock_zh_a_daily=fake.stock_zh_a_daily,
    )
    monkeypatch.setitem(sys.modules, "akshare", module)
    metadata = tmp_path / "universe.csv"
    metadata.write_text(
        "symbol,name,industry,list_date\n000001.SZ,平安银行,银行,1991-04-03\n",
        encoding="utf-8",
    )

    snapshot = AkshareProvider(("000001.SZ",), metadata_path=metadata).load(end)

    assert {bar.symbol for bar in snapshot.bars} == {"000001.SZ"}
    assert fake.history_calls[0]["symbol"] == "000001"
    audit = snapshot.metadata["universe_selection"]
    assert audit["mode"] == "configured_fallback"
    assert audit["fallback_reason"] == "dynamic_industry_metadata_unavailable"
    assert audit["dynamic_attempt"]["selected_symbols"] == ["600000.SH"]
    assert snapshot.metadata["security_metadata"]["sources"] == {
        "000001.SZ": "configured_static_fallback"
    }


def test_dynamic_listing_age_filter_runs_before_history_requests(monkeypatch):
    end = date.today()
    history_symbols: list[str] = []

    def info(symbol: str):
        listed = end - timedelta(days=30 if symbol == "000001" else 1_000)
        return Frame([
            {"item": "股票简称", "value": symbol},
            {"item": "行业", "value": "测试行业"},
            {"item": "上市时间", "value": listed.strftime("%Y%m%d")},
        ])

    def history(**kwargs):
        history_symbols.append(kwargs["symbol"])
        records = []
        for offset in range(65):
            day = end - timedelta(days=64 - offset)
            records.append({"日期": day.isoformat(), "开盘": 10, "收盘": 10.2,
                            "最高": 10.5, "最低": 9.8, "成交量": 1_000_000,
                            "成交额": 10_200_000})
        return Frame(records)

    module = SimpleNamespace(
        stock_zh_a_spot_em=lambda: Frame([
            {"代码": "000001", "名称": "新股样本", "最新价": 10,
             "成交额": 500_000_000, "60日涨跌幅": 20},
            {"代码": "600000", "名称": "成熟样本", "最新价": 12,
             "成交额": 400_000_000, "60日涨跌幅": 10},
        ]),
        stock_individual_info_em=info,
        stock_zh_a_hist=history,
    )
    monkeypatch.setitem(sys.modules, "akshare", module)

    snapshot = AkshareProvider(("600000.SH",), dynamic_universe_limit=2).load(end)

    assert history_symbols == ["600000"]
    assert snapshot.expected_symbols == 1
    listing_filter = snapshot.metadata["universe_selection"]["listing_age_filter"]
    assert listing_filter["excluded_symbols"] == ["000001.SZ"]
    assert listing_filter["minimum_calendar_days"] == 120


def test_historical_as_of_never_uses_current_all_a_snapshot(monkeypatch):
    end = date.today() - timedelta(days=1)
    fake = FakeAkshare(end)
    spot_calls = 0

    def spot():
        nonlocal spot_calls
        spot_calls += 1
        return Frame([])

    module = SimpleNamespace(
        stock_zh_a_spot_em=spot,
        stock_individual_info_em=fake.stock_individual_info_em,
        stock_zh_a_hist=fake.stock_zh_a_hist,
        stock_zh_a_daily=fake.stock_zh_a_daily,
    )
    monkeypatch.setitem(sys.modules, "akshare", module)

    snapshot = AkshareProvider(("000001.SZ",)).load(end)

    assert spot_calls == 0
    assert snapshot.metadata["universe_selection"]["fallback_reason"] == (
        "current_snapshot_not_valid_for_historical_as_of"
    )
