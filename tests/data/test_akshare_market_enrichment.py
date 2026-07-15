from __future__ import annotations

from datetime import date, timedelta

from quant_system.models import Bar
from quant_system.providers import AkshareProvider


class Frame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient="records"):
        assert orient == "records"
        return self.records


class EnrichmentApi:
    def __init__(self, end: date):
        self.end = end

    def _series(self, field="最新价", base=100.0):
        return Frame([
            {"日期": (self.end - timedelta(days=64 - index)).isoformat(), field: base + index}
            for index in range(65)
        ])

    def index_global_hist_em(self, **kwargs):
        assert kwargs["symbol"] in {"标普500", "纳斯达克", "日经225", "恒生指数", "COMEX黄金"}
        return self._series()

    def forex_hist_em(self, **kwargs):
        assert kwargs == {"symbol": "USDCNH"}
        return self._series(base=7.0)

    def bond_zh_us_rate(self, **kwargs):
        assert "start_date" in kwargs
        return self._series(field="美国国债收益率10年", base=3.0)

    def index_option_300etf_qvix(self):
        return self._series(field="close", base=15.0)

    def stock_a_ttm_lyr(self):
        return self._series(field="middlePETTM", base=15.0)


def bars(end: date):
    output = []
    for symbol_index, symbol in enumerate(("000001.SZ", "000002.SZ")):
        for index in range(65):
            day = end - timedelta(days=64 - index)
            close = 10 + symbol_index + index * .02
            output.append(Bar(symbol, symbol, day, close, close * 1.01, close * .99, close,
                              1_000_000, 10_000_000 + index * 100_000,
                              "银行", "银行"))
    return output


def test_public_enrichment_expresses_every_required_global_and_flow_dimension():
    end = date(2026, 7, 14)
    inputs, audit = AkshareProvider(("000001.SZ",))._public_market_enrichment(
        EnrichmentApi(end), end, bars(end)
    )

    assert inputs["global_components_available"] == 8
    assert inputs["global_components_total"] == 8
    assert inputs["global_risk_quality"] == "multi_asset_public_proxy"
    assert 0 <= inputs["global_risk_score"] <= 100
    assert audit["global_risk"]["components"].keys() >= {
        "sp500", "nasdaq", "nikkei225", "hang_seng", "usd_cnh",
        "us_10y_yield", "china_volatility", "gold",
    }
    assert audit["fund_flow"]["status"] == "available"
    assert audit["fund_flow"]["quality"] == "watchlist_turnover_breadth_proxy"
    assert "不是券商账户流向" in audit["fund_flow"]["warning"]
    assert audit["valuation"]["status"] == "available"


def test_public_enrichment_fails_each_optional_dimension_to_explicit_neutral():
    end = date(2026, 7, 14)
    inputs, audit = AkshareProvider(("000001.SZ",))._public_market_enrichment(
        object(), end, bars(end)
    )

    assert inputs["global_risk_score"] == 50
    assert inputs["global_risk_quality"] == "neutral_missing"
    assert inputs["fund_flow_quality"] == "watchlist_turnover_breadth_proxy"
    assert inputs["valuation_quality"] == "neutral_missing"
    assert all(item["status"] == "unavailable"
               for item in audit["global_risk"]["components"].values())
    assert audit["valuation"]["status"] == "unavailable"
