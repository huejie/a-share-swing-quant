from __future__ import annotations

from datetime import date, timedelta
import sys
from types import SimpleNamespace

from quant_system.providers import TushareProvider


class Frame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient="records"):
        assert orient == "records"
        return self.records


def sessions_through(end: date) -> list[date]:
    days = [end - timedelta(days=offset) for offset in range(190)
            if (end - timedelta(days=offset)).weekday() < 5]
    return sorted(days)[-120:]


class EnrichedPro:
    def __init__(self, sessions: list[date]):
        self.sessions = sessions
        self.adj_requests: list[str] = []

    def trade_cal(self, **_kwargs):
        next_day = self.sessions[-1] + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        return Frame([{"cal_date": day.strftime("%Y%m%d"), "is_open": 1}
                      for day in [*self.sessions, next_day]])

    def stock_basic(self, **kwargs):
        assert "industry" in kwargs["fields"]
        return Frame([{"ts_code": "000001.SZ", "name": "平安银行", "market": "主板",
                       "industry": "银行", "list_date": "19910403"}])

    def daily(self, **kwargs):
        return Frame([{"ts_code": "000001.SZ", "trade_date": kwargs["trade_date"], "open": 10,
                       "high": 11, "low": 9, "close": 10.5, "vol": 12.5, "amount": 131.25}])

    def adj_factor(self, **kwargs):
        self.adj_requests.append(kwargs["trade_date"])
        return Frame([{"ts_code": "000001.SZ", "trade_date": kwargs["trade_date"], "adj_factor": 2.5}])

    def daily_basic(self, **_kwargs):
        return Frame([{"ts_code": "000001.SZ", "pe_ttm": 10.0, "pb": 1.0,
                       "turnover_rate": 2.0, "volume_ratio": 1.1,
                       "total_mv": 200_000.0, "circ_mv": 180_000.0}])

    def index_daily(self, **_kwargs):
        rows = []
        for offset, day in enumerate(self.sessions[-61:]):
            rows.append({"ts_code": "000300.SH", "trade_date": day.strftime("%Y%m%d"),
                         "close": 1000 + offset * 2, "pct_chg": 0.2})
        return Frame(rows)

    def index_global(self, **kwargs):
        # Three documented international-major-index codes are requested
        # independently; this fixture returns reproducible histories for each.
        rows = []
        for offset, day in enumerate(self.sessions[-61:]):
            rows.append({"ts_code": kwargs["ts_code"], "trade_date": day.strftime("%Y%m%d"),
                         "close": 2000 + offset * 3, "pct_chg": 0.15})
        return Frame(rows)

    def stk_limit(self, **kwargs):
        return Frame([{"ts_code": "000001.SZ", "trade_date": kwargs["trade_date"],
                       "pre_close": 9.55, "up_limit": 10.5, "down_limit": 8.6}])

    def suspend_d(self, **_kwargs):
        return Frame([])


def install(monkeypatch, pro) -> None:
    monkeypatch.setitem(sys.modules, "tushare", SimpleNamespace(pro_api=lambda _token: pro))


def test_v2_maps_industry_adjustment_valuation_and_index_proxies(monkeypatch):
    end = date(2026, 7, 3)
    pro = EnrichedPro(sessions_through(end))
    install(monkeypatch, pro)

    snapshot = TushareProvider("token").load(end)

    assert snapshot.expected_symbols == 1
    assert all(bar.industry == "银行" and bar.theme == "银行" for bar in snapshot.bars)
    assert all(bar.adj_factor == 2.5 for bar in snapshot.bars)
    assert all(bar.quality == 50 and bar.catalyst == 50 for bar in snapshot.bars)
    assert len(pro.adj_requests) == 120
    assert snapshot.metadata["theme_mapping"]["status"] == "industry_fallback"
    assert snapshot.metadata["enrichments"]["adj_factor"]["status"] == "available"
    assert snapshot.metadata["enrichments"]["daily_basic"]["status"] == "available"
    assert snapshot.metadata["enrichments"]["index_daily"]["status"] == "available"
    market = snapshot.metadata["market_inputs"]
    assert market["valuation_score"] > 50
    assert market["median_pe_ttm"] == 10
    assert market["domestic_index_quality"] == "domestic_index_proxy"
    assert market["global_risk_quality"] == "international_index_basket_proxy"
    assert market["global_index_codes"] == ["HSI", "IXIC", "SPX"]
    assert market["fund_flow_score"] == 50
    assert market["fund_flow_quality"] == "neutral_missing"
    latest = max(snapshot.bars, key=lambda bar: bar.day)
    assert latest.limit_up is True and latest.limit_down is False
    assert sum(bar.limit_up for bar in snapshot.bars) == 1
    assert snapshot.metadata["enrichments"]["stk_limit"]["status"] == "available"
    assert snapshot.metadata["enrichments"]["suspend_d"]["status"] == "available"
    assert snapshot.metadata["simulation_matching_ready"] is True
    assert snapshot.metadata["next_trading_day"] == "2026-07-06"
    assert snapshot.metadata["trading_calendar"]["calendar_fallback"] is False
    assert snapshot.metadata["production_ready"] is False
    assert snapshot.metadata["pit_verified"] is False
    assert snapshot.metadata["research_eligible"] is False


class RestrictedPro(EnrichedPro):
    def stock_basic(self, **_kwargs):
        return Frame([{"ts_code": "000001.SZ", "name": "平安银行", "market": "主板",
                       "industry": None, "list_date": "19910403"}])

    def adj_factor(self, **_kwargs):
        raise RuntimeError("permission denied")

    def daily_basic(self, **_kwargs):
        raise RuntimeError("permission denied")

    def index_daily(self, **_kwargs):
        raise RuntimeError("permission denied")

    def index_global(self, **_kwargs):
        raise RuntimeError("permission denied")

    def stk_limit(self, **_kwargs):
        raise RuntimeError("permission denied")

    def suspend_d(self, **_kwargs):
        raise RuntimeError("permission denied")


def test_v2_optional_permission_failures_are_explicit_neutral_degradations(monkeypatch):
    end = date(2026, 7, 3)
    install(monkeypatch, RestrictedPro(sessions_through(end)))

    snapshot = TushareProvider("token").load(end)

    assert all(bar.theme == "行业未分类" and bar.industry == "行业未分类" for bar in snapshot.bars)
    assert all(bar.adj_factor == 1.0 for bar in snapshot.bars)
    enrichments = snapshot.metadata["enrichments"]
    assert enrichments["adj_factor"]["status"] == "unavailable"
    assert enrichments["adj_factor"]["reason"] == "request_or_permission_failed"
    assert enrichments["adj_factor"]["missing_bar_rows"] == 120
    assert enrichments["daily_basic"]["status"] == "unavailable"
    assert enrichments["index_daily"]["status"] == "unavailable"
    assert enrichments["index_global"]["status"] == "unavailable"
    assert enrichments["stk_limit"]["status"] == "unavailable"
    assert enrichments["suspend_d"]["status"] == "unavailable"
    assert snapshot.metadata["simulation_matching_ready"] is False
    assert snapshot.metadata["market_inputs"]["valuation_quality"] == "neutral_missing"
    assert snapshot.metadata["market_inputs"]["global_risk_quality"] == "neutral_missing"
    assert snapshot.metadata["theme_mapping"]["missing_symbols"] == 1
    assert snapshot.metadata["production_ready"] is False
    assert snapshot.metadata["pit_reconstruction"] is False


class OptionalMarketRestrictedPro(EnrichedPro):
    def index_global(self, **_kwargs):
        raise RuntimeError("permission denied")

    def stk_limit(self, **_kwargs):
        raise RuntimeError("permission denied")

    def suspend_d(self, **_kwargs):
        raise RuntimeError("permission denied")


def test_paid_constraints_and_global_index_fail_without_breaking_base_observation(monkeypatch):
    end = date(2026, 7, 3)
    install(monkeypatch, OptionalMarketRestrictedPro(sessions_through(end)))

    snapshot = TushareProvider("token").load(end)

    assert len(snapshot.bars) == 120
    assert snapshot.metadata["enrichments"]["adj_factor"]["status"] == "available"
    assert snapshot.metadata["enrichments"]["stk_limit"]["reason"] == "request_or_permission_failed"
    assert snapshot.metadata["enrichments"]["suspend_d"]["reason"] == "request_or_permission_failed"
    assert snapshot.metadata["enrichments"]["index_global"]["status"] == "unavailable"
    assert snapshot.metadata["market_inputs"]["domestic_index_quality"] == "domestic_index_proxy"
    assert snapshot.metadata["market_inputs"]["global_risk_quality"] == "neutral_missing"
    assert not any(bar.limit_up or bar.limit_down or bar.suspended for bar in snapshot.bars)


class SuspendedWithoutDailyPro(EnrichedPro):
    def stock_basic(self, **_kwargs):
        return Frame([
            {"ts_code": "000001.SZ", "name": "平安银行", "market": "主板", "industry": "银行", "list_date": "19910403"},
            {"ts_code": "000002.SZ", "name": "万科A", "market": "主板", "industry": "房地产", "list_date": "19910129"},
        ])

    def suspend_d(self, **kwargs):
        return Frame([{"ts_code": "000002.SZ", "trade_date": kwargs["trade_date"],
                       "suspend_timing": "09:30", "suspend_type": "S"}])


def test_suspend_audit_retains_listed_symbol_without_fabricating_daily_bar(monkeypatch):
    end = date(2026, 7, 3)
    install(monkeypatch, SuspendedWithoutDailyPro(sessions_through(end)))

    snapshot = TushareProvider("token").load(end)

    assert {bar.symbol for bar in snapshot.bars} == {"000001.SZ"}
    assert snapshot.expected_symbols == 2
    suspend = snapshot.metadata["enrichments"]["suspend_d"]
    assert suspend["status"] == "available"
    assert suspend["suspended_count"] == 1
    assert suspend["suspended_without_daily"] == ["000002.SZ"]
