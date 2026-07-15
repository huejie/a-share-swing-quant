from __future__ import annotations

import csv
import json
import math
import os
import time as clock_time
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from .models import Bar, DataSnapshot
from .pit import PITRecord, PointInTimeStore, RawBatchManifest, SecurityHistory, ThemeMembership, parse_time

SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketDataProvider(ABC):
    name = "abstract"

    @abstractmethod
    def load(self, as_of: date | None = None) -> DataSnapshot: ...

    def status(self) -> dict:
        return {"provider": self.name, "available": True, "mode": "read-only research data"}


class DeterministicDemoProvider(MarketDataProvider):
    """Stable, realistic-looking synthetic market. Same date always produces same data."""

    name = "deterministic-demo"
    universe = (
        ("600519.SH", "贵州茅台", "大消费", "食品饮料", "主板", 1725.0, 78, 62),
        ("000858.SZ", "五粮液", "大消费", "食品饮料", "主板", 136.0, 74, 60),
        ("300750.SZ", "宁德时代", "新能源", "电力设备", "创业板", 256.0, 82, 78),
        ("002594.SZ", "比亚迪", "智能汽车", "汽车", "主板", 338.0, 81, 76),
        ("688981.SH", "中芯国际", "半导体", "电子", "科创板", 91.0, 73, 84),
        ("603986.SH", "兆易创新", "半导体", "电子", "主板", 133.0, 76, 82),
        ("000977.SZ", "浪潮信息", "人工智能", "计算机", "主板", 58.0, 71, 88),
        ("300308.SZ", "中际旭创", "人工智能", "通信", "创业板", 182.0, 79, 90),
        ("600036.SH", "招商银行", "高股息", "银行", "主板", 45.0, 84, 48),
        ("601088.SH", "中国神华", "高股息", "煤炭", "主板", 42.0, 80, 52),
        ("600276.SH", "恒瑞医药", "创新药", "医药生物", "主板", 57.0, 83, 79),
        ("300760.SZ", "迈瑞医疗", "创新药", "医药生物", "创业板", 246.0, 86, 69),
    )

    def load(self, as_of: date | None = None) -> DataSnapshot:
        end = as_of or date(2026, 7, 3)
        days: list[date] = []
        cursor = end - timedelta(days=150)
        while cursor <= end:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        bars: list[Bar] = []
        theme_drift = {"人工智能": .0025, "半导体": .0021, "创新药": .0015, "智能汽车": .0011,
                       "新能源": .0008, "高股息": .00045, "大消费": -.0001}
        for sidx, (symbol, name, theme, industry, board, base, quality, catalyst) in enumerate(self.universe):
            price = base * .82
            for idx, day in enumerate(days):
                cyclical = math.sin((idx + sidx * 3) / 7) * .006 + math.cos((idx + sidx) / 17) * .003
                ret = theme_drift[theme] + cyclical + ((sidx % 3) - 1) * .00015
                previous = price
                price = max(2.0, price * (1 + ret))
                op = previous * (1 + math.sin(idx + sidx) * .0015)
                spread = .009 + (sidx % 4) * .0015
                high = max(op, price) * (1 + spread)
                low = min(op, price) * (1 - spread * .85)
                volume = int((7_500_000 + sidx * 1_350_000) * (1 + .20 * math.sin(idx / 5 + sidx)))
                bars.append(Bar(symbol, name, day, round(op, 2), round(high, 2), round(low, 2),
                                round(price, 2), volume, round(volume * price, 2), theme, industry,
                                board=board, listed_days=1800 + sidx * 90, quality=quality, catalyst=catalyst))
        ts = datetime.combine(end, time(18, 0), SHANGHAI)
        return DataSnapshot(ts, bars, self.name, len(self.universe), {"synthetic": True, "stable": True,
            "market_inputs":{"global_risk_score":64.0,"fund_flow_score":58.0,"valuation_score":57.0,"source":"确定性演示快照"}})


class CsvProvider(MarketDataProvider):
    name = "csv"

    def __init__(self, path: str | Path): self.path = Path(path)

    def load(self, as_of: date | None = None) -> DataSnapshot:
        bars = []
        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                day = date.fromisoformat(row["date"])
                if as_of and day > as_of: continue
                bars.append(Bar(row["symbol"], row["name"], day, *[float(row[x]) for x in ("open","high","low","close")],
                                int(row["volume"]), float(row["amount"]), row["theme"], row["industry"],
                                board=row.get("board", "主板"), is_st=row.get("is_st", "false").lower()=="true",
                                suspended=row.get("suspended", "false").lower()=="true",
                                listed_days=int(row.get("listed_days", 1000)), quality=float(row.get("quality", 70)),
                                catalyst=float(row.get("catalyst", 60)),
                                is_delisting=self._bool(row.get("is_delisting")) if hasattr(self,"_bool") else row.get("is_delisting","false").lower()=="true",
                                regulatory_risk=row.get("regulatory_risk","false").lower()=="true",
                                audit_abnormal=row.get("audit_abnormal","false").lower()=="true",
                                event_risk=row.get("event_risk","false").lower()=="true",
                                adj_factor=float(row.get("adj_factor",1) or 1)))
        if not bars: raise ValueError("CSV contains no usable bars")
        latest = max(b.day for b in bars)
        return DataSnapshot(datetime.combine(latest, time(18), SHANGHAI), bars, self.name,
                            len({b.symbol for b in bars}), {"source": str(self.path), "production_ready": False,
                                                           "pit_verified": False, "authorization": False,
                                                           "warning": "generic CSV is prototype data"})


class LicensedCsvBundleProvider(MarketDataProvider):
    """Read a local licensed/PIT bundle whose claims are explicit and hash-verifiable.

    Required files are metadata.json, bars.csv, securities.csv and
    theme_memberships.csv.  metadata.json must carry ``authorization.authorized``
    and ``pit.verified``; neither is inferred from the directory name.
    """
    name = "licensed-csv-bundle"
    required_files = ("bars.csv", "securities.csv", "theme_memberships.csv")

    def __init__(self, root: str | Path):
        self.root = Path(root)
        metadata_path = self.root / "metadata.json"
        self.metadata = json.loads(metadata_path.read_text("utf-8")) if metadata_path.is_file() else {}

    def _manifest(self) -> RawBatchManifest | None:
        raw = self.metadata.get("manifest")
        if not isinstance(raw, dict): return None
        try: return RawBatchManifest(**raw)
        except (TypeError, ValueError): return None

    def _manifest_status(self) -> tuple[bool, tuple[str, ...]]:
        manifest = self._manifest()
        if manifest is None: return False, ("manifest_missing_or_invalid",)
        signed_metadata = {k: v for k, v in self.metadata.items() if k != "manifest"}
        return manifest.verify(self.root, signed_metadata)

    def status(self) -> dict:
        files_present = all((self.root / name).is_file() for name in self.required_files)
        manifest_valid, errors = self._manifest_status()
        authorization = self.metadata.get("authorization", {})
        pit = self.metadata.get("pit", {})
        authorized = authorization.get("authorized") is True and bool(authorization.get("scope"))
        pit_verified = pit.get("verified") is True and bool(pit.get("method"))
        datasets = self.metadata.get("datasets")
        dataset_metadata_valid = isinstance(datasets, dict) and bool(datasets) and all(
            isinstance(value, dict) and bool(value.get("as_of")) and "required" in value for value in datasets.values())
        ready = files_present and manifest_valid and authorized and pit_verified and dataset_metadata_valid
        return {"provider": self.name, "available": files_present, "manifest_valid": manifest_valid,
                "manifest_errors": list(errors), "authorized": authorized, "authorization_scope": authorization.get("scope"),
                "pit_verified": pit_verified, "production_ready": ready,
                "dataset_metadata_valid": dataset_metadata_valid,
                "reason": None if ready else "bundle requires valid hashes, explicit authorization, and PIT verification"}

    @staticmethod
    def _bool(value: str | None) -> bool:
        return str(value or "").strip().lower() in {"true", "1", "yes"}

    def _load_pit(self) -> PointInTimeStore:
        store = PointInTimeStore()
        with (self.root / "securities.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                store.securities.append(SecurityHistory(row["symbol"], row["name"], date.fromisoformat(row["listed_at"]),
                                                         date.fromisoformat(row["delisted_at"]) if row.get("delisted_at") else None,
                                                         row.get("board", "主板")))
        with (self.root / "theme_memberships.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                store.memberships.append(ThemeMembership(row["symbol"], row["theme"], date.fromisoformat(row["effective_from"]),
                                                         date.fromisoformat(row["effective_to"]) if row.get("effective_to") else None,
                                                         parse_time(row["published_at"]), parse_time(row["available_at"])))
        records_path = self.root / "pit_records.csv"
        if records_path.is_file():
            with records_path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    store.append(PITRecord(row["dataset"], row["entity_id"], parse_time(row["effective_at"]),
                                           parse_time(row["published_at"]), parse_time(row["available_at"]),
                                           json.loads(row.get("payload_json") or "{}"), int(row.get("revision") or 1),
                                           row.get("source_ref", "")))
        return store

    def load(self, as_of: date | None = None) -> DataSnapshot:
        status = self.status()
        if not status["available"]: raise RuntimeError("licensed CSV bundle is incomplete")
        end = as_of or date.today(); cutoff = datetime.combine(end, time(23, 59, 59), SHANGHAI)
        pit_store = self._load_pit(); active = {x.symbol: x for x in pit_store.universe_as_of(end)}
        historical = {x.symbol: x for x in pit_store.securities}
        bars: list[Bar] = []; visibility_fields_complete = True
        with (self.root / "bars.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = date.fromisoformat(row["date"])
                required_times = (row.get("published_at"), row.get("effective_at"), row.get("available_at"))
                if not all(required_times): visibility_fields_complete = False; continue
                if (day > end or parse_time(row["effective_at"]) > cutoff or
                        parse_time(row["available_at"]) > cutoff or parse_time(row["published_at"]) > cutoff): continue
                security = historical.get(row["symbol"])
                if security is None or security.listed_at > day or (security.delisted_at and day >= security.delisted_at): continue
                theme = pit_store.theme_as_of(row["symbol"], datetime.combine(day, time(23, 59, 59), SHANGHAI))
                if theme is None: continue
                bars.append(Bar(row["symbol"], security.name, day, *[float(row[x]) for x in ("open","high","low","close")],
                                int(row["volume"]), float(row["amount"]), theme, row.get("industry", "未分类"),
                                board=security.board, is_st=self._bool(row.get("is_st")), suspended=self._bool(row.get("suspended")),
                                limit_up=self._bool(row.get("limit_up")), limit_down=self._bool(row.get("limit_down")),
                                listed_days=max(0, (day-security.listed_at).days), quality=float(row.get("quality", 70)),
                                catalyst=float(row.get("catalyst", 60)), is_delisting=self._bool(row.get("is_delisting")),
                                regulatory_risk=self._bool(row.get("regulatory_risk")), audit_abnormal=self._bool(row.get("audit_abnormal")),
                                event_risk=self._bool(row.get("event_risk")), adj_factor=float(row.get("adj_factor",1) or 1)))
        if not bars: raise ValueError("bundle contains no point-in-time visible bars for requested as_of")
        latest = max(x.day for x in bars); authorization = self.metadata.get("authorization", {})
        dataset_meta = self.metadata.get("datasets", {})
        production_ready = bool(status["production_ready"] and visibility_fields_complete)
        visible_records = pit_store.records_as_of(cutoff)
        metadata = {"bundle": str(self.root), "batch_id": self.metadata.get("batch_id"), **status,
                    "production_ready": production_ready, "visibility_fields_complete": visibility_fields_complete,
                    "datasets": dataset_meta, "historical_security_count": len(historical),
                    "market_inputs": self.metadata.get("market_inputs", {}),
                    "active_security_count": len(active), "authorization": authorization,
                    "as_of_reconstruction": True, "pit_records_visible_count": len(visible_records),
                    "pit_record_datasets": sorted({x.dataset for x in visible_records})}
        return DataSnapshot(datetime.combine(latest, time(18), SHANGHAI), bars, self.name, len(active), metadata)


class TushareProvider(MarketDataProvider):
    """Tushare Pro public-interface adapter for local research and simulation.

    This adapter deliberately does not claim point-in-time reconstruction or a
    redistribution/commercial-data licence.  It retrieves an explicit, recent
    120-session OHLCV window and rejects a requested non-trading date rather
    than silently substituting the preceding session.
    """
    name = "tushare"
    minimum_trading_days = 120
    token_environment_keys = ("QUANT_TUSHARE_TOKEN", "TUSHARE_TOKEN")

    def __init__(self, token: str | None = None, *, min_request_interval_seconds: float = 0.0,
                 sleeper=clock_time.sleep, clock=clock_time.monotonic):
        self.token = (token or "").strip() or None
        if min_request_interval_seconds < 0:
            raise ValueError("Tushare minimum request interval cannot be negative")
        self.min_request_interval_seconds = min_request_interval_seconds
        self._sleeper = sleeper
        self._clock = clock
        self._last_request_started_at: float | None = None

    @staticmethod
    def _records(frame: object, *, dataset: str) -> list[dict[str, object]]:
        """Convert a Tushare dataframe-like response without requiring pandas.

        Keeping this narrow makes provider failures actionable and permits
        deterministic tests with a small dataframe mock.
        """
        if frame is None:
            raise RuntimeError(f"Tushare {dataset} returned no response")
        to_dict = getattr(frame, "to_dict", None)
        if not callable(to_dict):
            raise RuntimeError(f"Tushare {dataset} returned an unsupported response type")
        try:
            records = to_dict(orient="records")
        except TypeError:
            records = to_dict("records")
        if not isinstance(records, list):
            raise RuntimeError(f"Tushare {dataset} returned an invalid response")
        return [dict(record) for record in records if isinstance(record, dict)]

    @staticmethod
    def _as_day(value: object, *, field: str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        raw = str(value or "").strip()
        try:
            return datetime.strptime(raw, "%Y%m%d").date() if len(raw) == 8 else date.fromisoformat(raw)
        except ValueError as exc:
            raise RuntimeError(f"Tushare returned invalid {field}: {raw!r}") from exc

    @staticmethod
    def _required(row: dict[str, object], field: str, *, dataset: str) -> object:
        value = row.get(field)
        if value is None or str(value).strip().lower() in {"", "nan", "none"}:
            raise RuntimeError(f"Tushare {dataset} response is missing required field {field!r}")
        return value

    @staticmethod
    def _board(market: object, symbol: str) -> str:
        declared = str(market or "").strip()
        if declared:
            return declared
        code = symbol.split(".", 1)[0]
        if code.startswith("688"):
            return "科创板"
        if code.startswith(("300", "301")):
            return "创业板"
        if symbol.endswith(".BJ") or code.startswith(("4", "8")):
            return "北交所"
        return "主板"

    def _pro_client(self):
        if not self.token:
            raise RuntimeError("Tushare token is not configured; set QUANT_TUSHARE_TOKEN (or TUSHARE_TOKEN)")
        try:
            import tushare as ts
        except ImportError as exc:
            raise RuntimeError("Tushare dependency is not installed; install project extra 'providers' (pip install .[providers])") from exc
        try:
            return ts.pro_api(self.token)
        except Exception as exc:  # The provider can reject an expired/invalid token here.
            # SDK exceptions may render request URLs or configuration.  Do not
            # preserve the cause: it can contain the token in server/API logs.
            raise RuntimeError("Tushare client initialization failed") from None

    def _request(self, client: object, method: str, **kwargs: object) -> list[dict[str, object]]:
        api = getattr(client, method, None)
        if not callable(api):
            raise RuntimeError(f"Tushare client does not support endpoint {method!r}")
        now = self._clock()
        if self._last_request_started_at is not None:
            remaining = self.min_request_interval_seconds - (now - self._last_request_started_at)
            if remaining > 0:
                self._sleeper(remaining)
        self._last_request_started_at = self._clock()
        try:
            response = api(**kwargs)
        except Exception as exc:
            # Never surface SDK error text or its exception chain; either can
            # embed request URLs and therefore a credential.
            raise RuntimeError(f"Tushare {method} request failed") from None
        return self._records(response, dataset=method)

    def _optional_request(self, client: object, method: str, **kwargs: object) -> tuple[list[dict[str, object]], str | None]:
        """Request an enrichment without disguising a permission/schema failure.

        Public Tushare accounts have materially different endpoint permissions.
        OHLCV remains the required core; enrichments report a stable reason code
        in snapshot metadata and never turn the provider into demo data.
        """
        if not callable(getattr(client, method, None)):
            return [], "endpoint_not_supported"
        try:
            return self._request(client, method, **kwargs), None
        except RuntimeError:
            return [], "request_or_permission_failed"

    @staticmethod
    def _finite_float(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _score(value: float) -> float:
        return round(max(0.0, min(100.0, value)), 1)

    @classmethod
    def _index_returns(cls, records: list[dict[str, object]]) -> tuple[int, float, float] | None:
        usable: list[tuple[date, float]] = []
        for row in records:
            close = cls._finite_float(row.get("close"))
            try:
                day = cls._as_day(row.get("trade_date"), field="trade_date")
            except RuntimeError:
                continue
            if close is not None and close > 0:
                usable.append((day, close))
        # Duplicate dates from an upstream revision are collapsed rather than
        # accidentally counting them as additional sessions.
        closes = sorted(dict(usable).items())
        if len(closes) < 61:
            return None
        last = closes[-1][1]
        return len(closes), last / closes[-21][1] - 1, last / closes[-61][1] - 1

    def _market_enrichment(self, client: object, start: date, end: date) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        """Return separate domestic and genuinely global market proxies."""
        records, error = self._optional_request(
            client, "index_daily", ts_code="000300.SH", start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"), fields="ts_code,trade_date,close,pct_chg",
        )
        domestic = self._index_returns(records)
        if error or domestic is None:
            domestic_market = {"domestic_index_score": 50.0, "domestic_index_quality": "neutral_missing",
                               "domestic_index_proxy": "沪深300指数20/60日趋势（缺失时中性）"}
            domestic_audit = {"status": "unavailable", "reason": error or "insufficient_history",
                              "rows": len(records), "proxy": "000300.SH"}
        else:
            rows, return20, return60 = domestic
            domestic_market = {"domestic_index_score": self._score(50 + return20 * 100 + return60 * 200),
                               "domestic_index_quality": "domestic_index_proxy",
                               "domestic_index_proxy": "沪深300指数20/60日趋势",
                               "domestic_index_code": "000300.SH",
                               "domestic_index_return_20d": round(return20, 6),
                               "domestic_index_return_60d": round(return60, 6)}
            domestic_audit = {"status": "available", "reason": None, "rows": rows, "proxy": "000300.SH"}

        # Tushare's documented international-major-index endpoint is optional
        # and frequently permission gated.  Require at least two independent
        # index series so a single regional market is never labelled global.
        global_codes = ("SPX", "IXIC", "HSI")
        global_returns: dict[str, tuple[int, float, float]] = {}
        global_errors: dict[str, str] = {}
        for code in global_codes:
            global_records, global_error = self._optional_request(
                client, "index_global", ts_code=code, start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"), fields="ts_code,trade_date,close,pct_chg",
            )
            if global_error:
                global_errors[code] = global_error
                # A missing endpoint or account permission applies to all
                # codes; do not spend two more calls proving the same failure.
                if global_error in {"endpoint_not_supported", "request_or_permission_failed"}:
                    break
                continue
            calculated = self._index_returns(global_records)
            if calculated is None:
                global_errors[code] = "insufficient_history"
            else:
                global_returns[code] = calculated
        if len(global_returns) < 2:
            global_market = {"global_risk_score": 50.0, "global_risk_quality": "neutral_missing",
                             "global_risk_proxy": "国际主要指数增强不可用，显式中性"}
            global_audit = {"status": "unavailable", "reason": "insufficient_global_series",
                            "required_series": 2, "available_series": sorted(global_returns),
                            "errors": global_errors, "requested_series": list(global_codes)}
        else:
            return20 = sum(value[1] for value in global_returns.values()) / len(global_returns)
            return60 = sum(value[2] for value in global_returns.values()) / len(global_returns)
            global_market = {"global_risk_score": self._score(50 + return20 * 100 + return60 * 200),
                             "global_risk_quality": "international_index_basket_proxy",
                             "global_risk_proxy": "可用国际主要指数等权20/60日趋势",
                             "global_index_codes": sorted(global_returns),
                             "global_index_return_20d": round(return20, 6),
                             "global_index_return_60d": round(return60, 6)}
            global_audit = {"status": "available", "reason": None, "required_series": 2,
                            "available_series": sorted(global_returns), "errors": global_errors,
                            "rows": {code: values[0] for code, values in global_returns.items()}}
        return {**domestic_market, **global_market}, domestic_audit, global_audit

    def _latest_trade_constraints(self, client: object, end: date) -> tuple[dict[str, tuple[float, float]], set[str], dict[str, object], dict[str, object]]:
        """Load optional next-session feasibility facts for the latest close."""
        limit_records, limit_error = self._optional_request(
            client, "stk_limit", trade_date=end.strftime("%Y%m%d"),
            fields="ts_code,trade_date,pre_close,up_limit,down_limit",
        )
        limits: dict[str, tuple[float, float]] = {}
        invalid_limit_rows = 0
        for row in limit_records:
            symbol = str(row.get("ts_code") or "").strip()
            up = self._finite_float(row.get("up_limit"))
            down = self._finite_float(row.get("down_limit"))
            try:
                row_day = self._as_day(row.get("trade_date"), field="trade_date")
            except RuntimeError:
                row_day = None
            if symbol and row_day == end and up is not None and down is not None and 0 < down < up:
                limits[symbol] = (up, down)
            else:
                invalid_limit_rows += 1
        limit_audit = {"status": "available" if limits else "unavailable",
                       "reason": limit_error or (None if limits else "empty_or_invalid_response"),
                       "rows": len(limits), "invalid_rows": invalid_limit_rows,
                       "method": "stk_limit_latest_close_match",
                       "warning": "可选付费端点；缺失时不推断涨跌停"}

        suspend_records, suspend_error = self._optional_request(
            client, "suspend_d", trade_date=end.strftime("%Y%m%d"),
            fields="ts_code,trade_date,suspend_timing,suspend_type",
        )
        suspended: set[str] = set()
        resumed: set[str] = set()
        invalid_suspend_rows = 0
        for row in suspend_records:
            symbol = str(row.get("ts_code") or "").strip()
            kind = str(row.get("suspend_type") or "").strip().upper()
            try:
                row_day = self._as_day(row.get("trade_date"), field="trade_date")
            except RuntimeError:
                row_day = None
            if not symbol or row_day != end or kind not in {"S", "R"}:
                invalid_suspend_rows += 1
            elif kind == "S":
                suspended.add(symbol)
            else:
                resumed.add(symbol)
        suspend_audit = {"status": "available" if suspend_error is None else "unavailable",
                         "reason": suspend_error, "rows": len(suspended) + len(resumed),
                         "suspended_count": len(suspended), "resumed_count": len(resumed),
                         "invalid_rows": invalid_suspend_rows,
                         "warning": "可选端点；无日线的停牌证券仅保留审计，不伪造行情"}
        return limits, suspended, limit_audit, {**suspend_audit, "resumed_symbols": sorted(resumed)}

    def _daily_basic_enrichment(self, client: object, end: date) -> tuple[dict[str, dict[str, object]], dict[str, object], dict[str, object]]:
        fields = "ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv"
        records, error = self._optional_request(client, "daily_basic", trade_date=end.strftime("%Y%m%d"), fields=fields)
        by_symbol: dict[str, dict[str, object]] = {}
        pe_values: list[float] = []
        pb_values: list[float] = []
        turnover_values: list[float] = []
        for row in records:
            symbol = str(row.get("ts_code") or "").strip()
            if not symbol:
                continue
            by_symbol[symbol] = row
            pe = self._finite_float(row.get("pe_ttm"))
            pb = self._finite_float(row.get("pb"))
            turnover = self._finite_float(row.get("turnover_rate"))
            if pe is not None and pe > 0:
                pe_values.append(pe)
            if pb is not None and pb > 0:
                pb_values.append(pb)
            if turnover is not None and turnover >= 0:
                turnover_values.append(turnover)
        if error or not by_symbol:
            market = {"valuation_score": 50.0, "valuation_quality": "neutral_missing",
                      "fund_flow_score": 50.0, "fund_flow_quality": "neutral_missing",
                      "daily_basic_proxy": "估值/换手截面缺失，使用显式中性值"}
            audit = {"status": "unavailable", "reason": error or "empty_response", "rows": 0}
            return {}, market, audit
        median_pe = median(pe_values) if pe_values else None
        median_pb = median(pb_values) if pb_values else None
        # An explicit coarse market-cheapness proxy.  Raw medians are retained
        # so downstream users can reproduce or replace the transform.
        valuation_score = 50.0
        if median_pe is not None:
            valuation_score += (25.0 - median_pe) * 1.2
        if median_pb is not None:
            valuation_score += (3.0 - median_pb) * 4.0
        market = {"valuation_score": self._score(valuation_score), "valuation_quality": "daily_basic_cross_section_proxy",
                  "median_pe_ttm": round(median_pe, 4) if median_pe is not None else None,
                  "median_pb": round(median_pb, 4) if median_pb is not None else None,
                  # Turnover is liquidity, not capital flow.  Keep fund-flow
                  # neutral instead of relabelling it as a fact we do not have.
                  "fund_flow_score": 50.0, "fund_flow_quality": "neutral_missing",
                  "median_turnover_rate": round(median(turnover_values), 4) if turnover_values else None,
                  "daily_basic_proxy": "全市场PE/PB估值截面；换手率仅作流动性观察，不冒充资金流"}
        audit = {"status": "available", "reason": None, "rows": len(by_symbol),
                 "pe_rows": len(pe_values), "pb_rows": len(pb_values), "turnover_rows": len(turnover_values)}
        return by_symbol, market, audit

    def _trading_days(self, client: object, end: date) -> tuple[list[date], date, dict[str, object]]:
        # Two calendar years comfortably covers 120 A-share sessions and avoids
        # guessing a calendar-day lookback around long mainland holidays.  A
        # short forward window supplies the real next session across statutory
        # holidays without contaminating the <= end historical window.
        start = end - timedelta(days=730)
        calendar_end = end + timedelta(days=20)
        records = self._request(client, "trade_cal", exchange="SSE", start_date=start.strftime("%Y%m%d"),
                                end_date=calendar_end.strftime("%Y%m%d"), fields="cal_date,is_open")
        open_days: list[date] = []
        for row in records:
            if str(row.get("is_open", "")).strip() in {"1", "1.0", "True", "true"}:
                open_days.append(self._as_day(self._required(row, "cal_date", dataset="trade_cal"), field="cal_date"))
        open_days = sorted(set(open_days))
        if end not in open_days:
            raise ValueError(f"requested as_of {end.isoformat()} is not an SSE trading day; no data was fabricated")
        historical = [day for day in open_days if day <= end]
        if len(historical) < self.minimum_trading_days:
            raise RuntimeError(f"Tushare trade calendar has only {len(historical)} open sessions through {end.isoformat()}; need {self.minimum_trading_days}")
        future = [day for day in open_days if day > end]
        if future:
            next_day = future[0]
            audit = {"status": "available", "source": "trade_cal.SSE", "calendar_fallback": False,
                     "requested_through": calendar_end.isoformat()}
        else:
            next_day = end + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            audit = {"status": "fallback", "source": "weekday_only", "calendar_fallback": True,
                     "reason": "trade_cal_forward_window_empty", "requested_through": calendar_end.isoformat()}
        return historical[-self.minimum_trading_days:], next_day, audit

    def status(self) -> dict:
        try:
            import tushare  # noqa: F401
            dependency_available = True
        except ImportError:
            dependency_available = False
        reason = None
        if not self.token:
            reason = "token not configured; set QUANT_TUSHARE_TOKEN (or TUSHARE_TOKEN)"
        elif not dependency_available:
            reason = "optional package not installed; install project extra 'providers'"
        return {"provider": self.name, "available": bool(self.token) and dependency_available,
                "dependency_available": dependency_available, "token_configured": bool(self.token), "reason": reason,
                "min_request_interval_seconds": self.min_request_interval_seconds,
                "mode": "public-interface research and simulation only", "prototype": True,
                "observation_only": True,
                "production_ready": False, "pit_verified": False, "authorization": False,
                "warning": "公开接口数据未验证历史成分、发布时间、修订版本或商业授权；不得作为生产PIT研究数据"}

    def load(self, as_of: date | None = None) -> DataSnapshot:
        end = as_of or date.today()
        client = self._pro_client()
        trading_days, next_trading_day, calendar_audit = self._trading_days(client, end)
        basics = self._request(client, "stock_basic", exchange="", list_status="L",
                               fields="ts_code,name,market,industry,list_date")
        basic_by_symbol: dict[str, dict[str, object]] = {}
        for row in basics:
            symbol = str(self._required(row, "ts_code", dataset="stock_basic")).strip()
            basic_by_symbol[symbol] = row
        if not basic_by_symbol:
            raise RuntimeError("Tushare stock_basic returned no listed securities")

        daily_basic_by_symbol, daily_basic_market, daily_basic_audit = self._daily_basic_enrichment(client, end)
        index_market, index_audit, global_index_audit = self._market_enrichment(client, trading_days[0], end)
        limits, suspended_symbols, limit_audit, suspend_audit = self._latest_trade_constraints(client, end)
        bars: list[Bar] = []
        returned_days: set[date] = set()
        skipped_unknown_symbols = 0
        missing_industry_symbols: set[str] = set()
        missing_adj_factor_rows = 0
        adj_factor_rows = 0
        adj_factor_error: str | None = None
        adj_factor_enabled = callable(getattr(client, "adj_factor", None))
        for trading_day in trading_days:
            adj_by_symbol: dict[str, float] = {}
            if adj_factor_enabled:
                adj_records, current_adj_error = self._optional_request(
                    client, "adj_factor", trade_date=trading_day.strftime("%Y%m%d"),
                    fields="ts_code,trade_date,adj_factor",
                )
                if current_adj_error:
                    adj_factor_error = current_adj_error
                    adj_factor_enabled = False
                else:
                    for adj_row in adj_records:
                        symbol = str(adj_row.get("ts_code") or "").strip()
                        factor = self._finite_float(adj_row.get("adj_factor"))
                        if symbol and factor is not None and factor > 0:
                            adj_by_symbol[symbol] = factor
                            adj_factor_rows += 1
            daily_records = self._request(client, "daily", trade_date=trading_day.strftime("%Y%m%d"),
                                          fields="ts_code,trade_date,open,high,low,close,vol,amount")
            if not daily_records:
                raise RuntimeError(f"Tushare daily returned no rows for trading day {trading_day.isoformat()}")
            day_has_usable_bar = False
            for row in daily_records:
                symbol = str(self._required(row, "ts_code", dataset="daily")).strip()
                basic = basic_by_symbol.get(symbol)
                if basic is None:
                    skipped_unknown_symbols += 1
                    continue
                row_day = self._as_day(self._required(row, "trade_date", dataset="daily"), field="trade_date")
                if row_day != trading_day:
                    raise RuntimeError(f"Tushare daily returned {row_day.isoformat()} for requested {trading_day.isoformat()}")
                list_date = self._as_day(self._required(basic, "list_date", dataset="stock_basic"), field="list_date")
                industry = str(basic.get("industry") or "").strip()
                if not industry or industry.lower() in {"nan", "none"}:
                    industry = "行业未分类"
                    missing_industry_symbols.add(symbol)
                adj_factor = adj_by_symbol.get(symbol)
                if adj_factor is None:
                    missing_adj_factor_rows += 1
                    adj_factor = 1.0
                name = str(self._required(basic, "name", dataset="stock_basic"))
                up_limit, down_limit = limits.get(symbol, (None, None)) if row_day == end else (None, None)
                close = float(self._required(row, "close", dataset="daily"))
                bars.append(Bar(symbol, str(self._required(basic, "name", dataset="stock_basic")), row_day,
                                *[float(self._required(row, field, dataset="daily")) for field in ("open", "high", "low", "close")],
                                int(float(self._required(row, "vol", dataset="daily")) * 100),
                                float(self._required(row, "amount", dataset="daily")) * 1000,
                                industry, industry, board=self._board(basic.get("market"), symbol),
                                is_st="ST" in name.upper(), is_delisting="退" in name,
                                suspended=row_day == end and symbol in suspended_symbols,
                                limit_up=up_limit is not None and abs(close - up_limit) <= .0051,
                                limit_down=down_limit is not None and abs(close - down_limit) <= .0051,
                                listed_days=max(0, (row_day - list_date).days), quality=50.0, catalyst=50.0,
                                adj_factor=adj_factor))
                day_has_usable_bar = True
                returned_days.add(row_day)
            if not day_has_usable_bar:
                raise RuntimeError(f"Tushare daily has no usable listed-security rows for {trading_day.isoformat()}")
        if returned_days != set(trading_days):
            missing = sorted(set(trading_days) - returned_days)
            raise RuntimeError(f"Tushare daily history is incomplete; missing {len(missing)} trading days")
        adj_status = "available" if missing_adj_factor_rows == 0 else "partial" if adj_factor_rows else "unavailable"
        latest_symbols = {bar.symbol for bar in bars if bar.day == end}
        limit_audit["latest_bar_coverage"] = len(set(limits) & latest_symbols)
        limit_audit["missing_latest_bar_rows"] = len(latest_symbols - set(limits))
        if limit_audit["status"] == "available" and limit_audit["missing_latest_bar_rows"]:
            limit_audit["status"] = "partial"
            limit_audit["reason"] = "incomplete_latest_symbol_coverage"
        suspend_audit["suspended_without_daily"] = sorted(suspended_symbols - latest_symbols)
        matching_ready=(limit_audit.get("status")=="available" and
                        suspend_audit.get("status")=="available")
        metadata = {**self.status(), "requested_as_of": end.isoformat(), "trading_days": [day.isoformat() for day in trading_days],
                    "next_trading_day": next_trading_day.isoformat(), "trading_calendar": calendar_audit,
                    "trading_day_count": len(trading_days), "stock_basic_count": len(basic_by_symbol),
                    "skipped_unknown_symbols": skipped_unknown_symbols, "public_data": True,
                    "pit_reconstruction": False, "research_eligible": False,
                    "simulation_matching_ready": matching_ready,
                    "theme_mapping": {"status": "industry_fallback", "source": "stock_basic.industry",
                                      "missing_symbols": len(missing_industry_symbols),
                                      "warning": "Tushare stock_basic不含题材历史；行业仅作题材分组回退且非PIT"},
                    "enrichments": {"adj_factor": {"status": adj_status, "reason": adj_factor_error or
                                                    ("endpoint_not_supported" if not callable(getattr(client, "adj_factor", None)) else None),
                                                    "rows": adj_factor_rows, "missing_bar_rows": missing_adj_factor_rows,
                                                    "fallback": "identity_1.0_with_explicit_missing_flag"},
                                    "daily_basic": daily_basic_audit, "index_daily": index_audit,
                                    "index_global": global_index_audit,
                                    "stk_limit": limit_audit, "suspend_d": suspend_audit},
                    "daily_basic_coverage": {"matched_symbols": len(daily_basic_by_symbol),
                                             "listed_symbols": len(basic_by_symbol)},
                    "market_inputs": {**index_market, **daily_basic_market,
                                      "source": "Tushare公开接口：行情宽度 + 国内/国际指数分离代理 + daily_basic估值截面；缺失项显式中性降级"},
                    "neutral_stock_fields": {"quality": 50.0, "catalyst": 50.0,
                                             "reason": "公开行情接口不提供可验证的财务质量/题材催化评分"}}
        return DataSnapshot(datetime.combine(end, time(18), SHANGHAI), bars, self.name,
                            len(basic_by_symbol), metadata)


class AkshareProvider(MarketDataProvider):
    name = "akshare"

    def __init__(self, symbols: tuple[str, ...] = (), *, metadata_path: str | Path | None = None,
                 min_request_interval_seconds: float = 0.0,
                 dynamic_universe: bool = True, dynamic_universe_limit: int = 60,
                 min_snapshot_turnover: float = 100_000_000.0,
                 min_listed_days: int = 120,
                 sleeper=clock_time.sleep, clock=clock_time.monotonic):
        if min_request_interval_seconds < 0:
            raise ValueError("AKShare minimum request interval cannot be negative")
        if not 1 <= dynamic_universe_limit <= 60:
            raise ValueError("AKShare dynamic universe limit must be between 1 and 60")
        if not math.isfinite(min_snapshot_turnover) or min_snapshot_turnover < 0:
            raise ValueError("AKShare minimum snapshot turnover must be a non-negative finite number")
        if min_listed_days < 0:
            raise ValueError("AKShare minimum listed days cannot be negative")
        self.symbols = symbols
        self.metadata_path = Path(metadata_path) if metadata_path else None
        self.min_request_interval_seconds = min_request_interval_seconds
        self.dynamic_universe = dynamic_universe
        self.dynamic_universe_limit = dynamic_universe_limit
        self.min_snapshot_turnover = min_snapshot_turnover
        self.min_listed_days = min_listed_days
        self._sleeper = sleeper
        self._clock = clock
        self._last_request_started_at: float | None = None

    @staticmethod
    def _records(frame: object, *, dataset: str) -> list[dict[str, object]]:
        if frame is None:
            raise RuntimeError(f"AKShare {dataset} returned no response")
        to_dict = getattr(frame, "to_dict", None)
        if not callable(to_dict):
            raise RuntimeError(f"AKShare {dataset} returned an unsupported response type")
        try:
            records = to_dict(orient="records")
        except TypeError:
            records = to_dict("records")
        if not isinstance(records, list):
            raise RuntimeError(f"AKShare {dataset} returned an invalid response")
        return [dict(record) for record in records if isinstance(record, dict)]

    @staticmethod
    def _value(row: dict[str, object], aliases: tuple[str, ...], *, dataset: str) -> object:
        for field in aliases:
            value = row.get(field)
            if value is not None and str(value).strip().lower() not in {"", "nan", "none", "-", "--", "—"}:
                return value
        raise RuntimeError(f"AKShare {dataset} response is missing required field {aliases[0]!r}")

    @staticmethod
    def _day(value: object, *, dataset: str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        raw = str(value or "").strip()
        try:
            if len(raw) == 8 and raw.isdigit():
                return datetime.strptime(raw, "%Y%m%d").date()
            return date.fromisoformat(raw[:10])
        except ValueError:
            raise RuntimeError(f"AKShare {dataset} returned an invalid date") from None

    def _request(self, api: object, method: str, **kwargs: object) -> list[dict[str, object]]:
        endpoint = getattr(api, method, None)
        if not callable(endpoint):
            raise RuntimeError(f"AKShare endpoint {method!r} is unavailable")
        now = self._clock()
        if self._last_request_started_at is not None:
            remaining = self.min_request_interval_seconds - (now - self._last_request_started_at)
            if remaining > 0:
                self._sleeper(remaining)
        self._last_request_started_at = self._clock()
        try:
            return self._records(endpoint(**kwargs), dataset=method)
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(f"AKShare {method} request failed") from None

    @staticmethod
    def _score(value: float) -> float:
        return round(max(0.0, min(100.0, value)), 1)

    @classmethod
    def _series_signal(
        cls,
        records: list[dict[str, object]],
        end: date,
        *,
        dataset: str,
        date_fields: tuple[str, ...] = ("日期", "date"),
        value_fields: tuple[str, ...] = ("最新价", "收盘", "close"),
        direction: float = 1.0,
        use_difference: bool = False,
    ) -> dict[str, object]:
        """Turn a public macro/market series into an auditable bounded risk score.

        ``direction`` is positive for risk-on series (equities/copper) and
        negative for risk-off series (USD/CNH, yields, volatility/gold).  The
        helper deliberately exposes its inputs and transformation; it never
        labels a public price proxy as a directly observed investor flow.
        """
        values: list[tuple[date, float]] = []
        for row in records:
            try:
                day = cls._day(cls._value(row, date_fields, dataset=dataset), dataset=dataset)
                value = float(cls._value(row, value_fields, dataset=dataset))
            except (RuntimeError, TypeError, ValueError, OverflowError):
                continue
            if day <= end and math.isfinite(value) and value > 0:
                values.append((day, value))
        values = sorted(dict(values).items())
        if len(values) < 21:
            raise RuntimeError(f"AKShare {dataset} has fewer than 21 usable observations")
        latest_day, latest = values[-1]
        prior20 = values[-21][1]
        prior60 = values[-61][1] if len(values) >= 61 else values[0][1]
        if use_difference:
            signal20 = latest - prior20
            signal60 = latest - prior60
            raw_score = 50 + direction * (signal20 * 4 + signal60 * 1.5)
        else:
            signal20 = latest / prior20 - 1
            signal60 = latest / prior60 - 1
            raw_score = 50 + direction * (signal20 * 180 + signal60 * 80)
        return {
            "status": "available",
            "as_of": latest_day.isoformat(),
            "observations": len(values),
            "latest": round(latest, 6),
            "change_20d": round(signal20, 6),
            "change_60d": round(signal60, 6),
            "score": cls._score(raw_score),
            "transformation": "50 + signed(20d×180 + 60d×80)" if not use_difference else
                              "50 + signed(20d差×4 + 60d差×1.5)",
        }

    def _optional_series(
        self,
        api: object,
        end: date,
        *,
        method: str,
        kwargs: dict[str, object] | None = None,
        date_fields: tuple[str, ...] = ("日期", "date"),
        value_fields: tuple[str, ...] = ("最新价", "收盘", "close"),
        direction: float = 1.0,
        use_difference: bool = False,
        label: str,
    ) -> dict[str, object]:
        try:
            records = self._request(api, method, **(kwargs or {}))
            return {
                "label": label,
                "source": f"AKShare.{method}",
                **self._series_signal(
                    records, end, dataset=method, date_fields=date_fields,
                    value_fields=value_fields, direction=direction,
                    use_difference=use_difference,
                ),
            }
        except RuntimeError as exc:
            return {
                "label": label,
                "source": f"AKShare.{method}",
                "status": "unavailable",
                "score": 50.0,
                "error": str(exc),
                "warning": "公开增强接口不可用，本分项显式中性，不用旧值冒充当期数据",
            }

    def _public_market_enrichment(self, api: object, end: date, bars: list[Bar]) -> tuple[dict, dict]:
        global_components = {
            "sp500": self._optional_series(api, end, method="index_global_hist_em",
                kwargs={"symbol": "标普500"}, direction=1, label="标普500"),
            "nasdaq": self._optional_series(api, end, method="index_global_hist_em",
                kwargs={"symbol": "纳斯达克"}, direction=1, label="纳斯达克"),
            "nikkei225": self._optional_series(api, end, method="index_global_hist_em",
                kwargs={"symbol": "日经225"}, direction=1, label="日经225"),
            "hang_seng": self._optional_series(api, end, method="index_global_hist_em",
                kwargs={"symbol": "恒生指数"}, direction=1, label="恒生指数"),
            "usd_cnh": self._optional_series(api, end, method="forex_hist_em",
                kwargs={"symbol": "USDCNH"}, direction=-1, label="美元兑离岸人民币"),
            "us_10y_yield": self._optional_series(api, end, method="bond_zh_us_rate",
                kwargs={"start_date": (end - timedelta(days=400)).strftime("%Y%m%d")},
                value_fields=("美国国债收益率10年",), direction=-1, use_difference=True,
                label="美国10年国债收益率"),
            "china_volatility": self._optional_series(api, end, method="index_option_300etf_qvix",
                value_fields=("close", "收盘价", "收盘"), direction=-1, label="沪深300ETF期权波动率"),
            "gold": self._optional_series(api, end, method="index_global_hist_em",
                kwargs={"symbol": "COMEX黄金"}, direction=-1, label="COMEX黄金"),
        }
        available_global = [float(item["score"]) for item in global_components.values()
                            if item["status"] == "available"]
        global_quality = ("multi_asset_public_proxy" if len(available_global) == len(global_components)
                          else "partial_multi_asset_public_proxy" if available_global else "neutral_missing")
        global_score = self._score(mean(available_global)) if available_global else 50.0

        # A verifiable transaction-structure proxy derived from the configured
        # observation pool.  This is intentionally *not* called 主力资金流入.
        histories: dict[str, list[Bar]] = {}
        for bar in bars:
            histories.setdefault(bar.symbol, []).append(bar)
        for values in histories.values():
            values.sort(key=lambda item: item.day)
        eligible = [values for values in histories.values() if len(values) >= 21]
        if eligible:
            latest_amount = sum(values[-1].amount for values in eligible)
            amount20 = mean(sum(values[-offset].amount for values in eligible) for offset in range(1, 21))
            advancing_amount = sum(values[-1].amount for values in eligible
                                   if values[-1].close > values[-2].close)
            advance_share = advancing_amount / latest_amount if latest_amount else .5
            positive20 = mean(1.0 if values[-1].close > values[-21].close else 0.0 for values in eligible)
            amount_ratio = latest_amount / amount20 if amount20 else 1.0
            fund_score = self._score(50 + (amount_ratio - 1) * 25 + (advance_share - .5) * 30 +
                                     (positive20 - .5) * 20)
            fund_flow = {
                "status": "available", "score": fund_score,
                "quality": "watchlist_turnover_breadth_proxy",
                "as_of": max(values[-1].day for values in eligible).isoformat(),
                "amount_ratio_vs_20d": round(amount_ratio, 4),
                "advancing_amount_share": round(advance_share, 4),
                "positive_20d_breadth": round(positive20, 4),
                "universe_symbols": len(eligible),
                "source": "本次有界观察池成交额结构、上涨成交额占比与20日广度",
                "warning": "这是可复算的资金活跃度代理，不是券商账户流向或所谓主力净流入",
            }
        else:
            fund_flow = {"status": "unavailable", "score": 50.0, "quality": "neutral_missing",
                         "warning": "观察池历史不足，资金代理显式中性"}

        valuation = self._optional_series(api, end, method="stock_a_ttm_lyr",
            value_fields=("middlePETTM", "中位数市盈率TTM", "市盈率TTM中位数", "middlePELYR"),
            direction=-1, label="全A估值中位数")
        # Valuation levels are not return series, so keep the optional series'
        # transparent trend score and label the limitation explicitly.
        valuation["warning"] = (valuation.get("warning", "") +
            "；估值分仅反映公开全A中位数估值的20/60日变化，不代表绝对便宜或昂贵").strip("；")

        market_inputs = {
            "global_risk_score": global_score,
            "global_risk_quality": global_quality,
            "global_risk_proxy": "全球股指、美元兑人民币、利率、波动率和商品的可用分项等权风险代理",
            "global_components_available": len(available_global),
            "global_components_total": len(global_components),
            "fund_flow_score": float(fund_flow["score"]),
            "fund_flow_quality": fund_flow["quality"],
            "fund_flow_proxy": fund_flow.get("source", fund_flow.get("warning")),
            "valuation_score": float(valuation["score"]),
            "valuation_quality": "public_trend_proxy" if valuation["status"] == "available" else "neutral_missing",
            "source": "AKShare公开多资产增强；各分项失败时独立中性降级，分数和原始代理均留审计",
        }
        audit = {"global_risk": {"status": "available" if available_global else "unavailable",
                                  "quality": global_quality, "score": global_score,
                                  "components": global_components},
                 "fund_flow": fund_flow, "valuation": valuation}
        return market_inputs, audit

    def _security_info(self, api: object, symbol: str) -> tuple[str, str, date]:
        records = self._request(api, "stock_individual_info_em", symbol=symbol.split(".", 1)[0])
        values: dict[str, object] = {}
        for row in records:
            item = str(row.get("item") or row.get("项目") or row.get("指标") or "").strip()
            if not item:
                continue
            value = row.get("value") if "value" in row else row.get("值")
            values[item] = value
        industry = str(self._value(values, ("行业",), dataset="stock_individual_info_em")).strip()
        listed_at = self._day(self._value(values, ("上市时间", "上市日期"), dataset="stock_individual_info_em"),
                              dataset="stock_individual_info_em")
        name_value = values.get("股票简称") or values.get("证券简称")
        name = str(name_value).strip() if name_value is not None and str(name_value).strip() else symbol
        return name, industry, listed_at

    def _configured_security_metadata(self) -> dict[str, tuple[str, str, date]]:
        if self.metadata_path is None:
            return {}
        if not self.metadata_path.is_file():
            raise RuntimeError(f"AKShare configured metadata file does not exist: {self.metadata_path}")
        result: dict[str, tuple[str, str, date]] = {}
        with self.metadata_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"symbol", "name", "industry", "list_date"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise RuntimeError("AKShare configured metadata must contain symbol,name,industry,list_date")
            for row in reader:
                symbol = str(row.get("symbol") or "").strip().upper()
                name = str(row.get("name") or "").strip()
                industry = str(row.get("industry") or "").strip()
                if not symbol or not name or not industry:
                    raise RuntimeError("AKShare configured metadata contains an empty required value")
                if symbol in result:
                    raise RuntimeError(f"AKShare configured metadata contains duplicate symbol {symbol}")
                listed_at = self._day(row.get("list_date"), dataset="configured_security_metadata")
                result[symbol] = (name, industry, listed_at)
        return result

    @staticmethod
    def _sina_symbol(symbol: str) -> str:
        code = symbol.split(".", 1)[0]
        if code.startswith("6"):
            return f"sh{code}"
        if code.startswith(("0", "3")):
            return f"sz{code}"
        raise RuntimeError(f"AKShare Sina fallback does not support board for {symbol}; observation is blocked")

    @staticmethod
    def _canonical_mainland_a_symbol(value: object) -> str | None:
        """Return a canonical SSE/SZSE A-share symbol, excluding BSE/B shares.

        ``stock_zh_a_spot_em`` currently combines Shanghai, Shenzhen and
        Beijing A shares.  The provider intentionally accepts only the known
        SSE ``6xxxxx`` and SZSE ``0xxxxx/3xxxxx`` namespaces.  Beijing
        ``4/8/92`` codes and Shenzhen/Shanghai B-share ``200/900`` codes are
        therefore rejected before any per-symbol request is made.
        """
        code = str(value or "").strip()
        if len(code) != 6 or not code.isdigit():
            return None
        if code.startswith("6"):
            return f"{code}.SH"
        if code.startswith(("000", "001", "002", "003", "300", "301")):
            return f"{code}.SZ"
        return None

    @staticmethod
    def _optional_number(row: dict[str, object], aliases: tuple[str, ...]) -> float | None:
        for field in aliases:
            value = row.get(field)
            if value is None or str(value).strip().lower() in {"", "nan", "none", "-", "--", "—"}:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number):
                return number
        return None

    def _configured_universe_audit(self, end: date, reason: str, error: str | None = None) -> dict:
        return {
            "mode": "configured_fallback",
            "source": "QUANT_AKSHARE_SYMBOLS",
            "requested_as_of": end.isoformat(),
            "selection_snapshot_kind": "configured_current_watchlist",
            "selection_snapshot_is_pit": False,
            "production_ready": False,
            "simulation_matching_ready": False,
            "fallback_used": True,
            "fallback_reason": reason,
            "fallback_error": error,
            "configured_fallback_count": len(self.symbols),
            "selected_count": len(self.symbols),
            "selected_symbols": list(self.symbols),
            "warning": "配置池是运行期观察范围，不是历史时点成分；不得用于PIT回测或模拟撮合",
        }

    def _select_observation_universe(self, api: object, end: date, *, allow_current_snapshot: bool) -> tuple[tuple[str, ...], dict]:
        """Stage one: reduce the current all-A snapshot to at most 60 names.

        This is deliberately a *current-snapshot* screen.  A caller asking for
        a historical ``as_of`` date is routed to the configured watchlist so a
        present-day ranking can never masquerade as a point-in-time universe.
        """
        if not self.dynamic_universe:
            return self.symbols, self._configured_universe_audit(end, "dynamic_selection_disabled")
        if not allow_current_snapshot:
            return self.symbols, self._configured_universe_audit(
                end, "current_snapshot_not_valid_for_historical_as_of"
            )
        try:
            records = self._request(api, "stock_zh_a_spot_em")
        except RuntimeError as exc:
            return self.symbols, self._configured_universe_audit(
                end, "all_a_snapshot_unavailable", str(exc)
            )

        rejected = {
            "invalid_or_unsupported_code": 0,
            "duplicate_symbol": 0,
            "st_or_delisting_name": 0,
            "invalid_price": 0,
            "below_min_turnover": 0,
        }
        candidates: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in records:
            symbol = self._canonical_mainland_a_symbol(row.get("代码") or row.get("code"))
            if symbol is None:
                rejected["invalid_or_unsupported_code"] += 1
                continue
            if symbol in seen:
                rejected["duplicate_symbol"] += 1
                continue
            seen.add(symbol)
            name = str(row.get("名称") or row.get("name") or symbol).strip()
            upper_name = name.upper()
            if "ST" in upper_name or "退" in name:
                rejected["st_or_delisting_name"] += 1
                continue
            price = self._optional_number(row, ("最新价", "close", "price"))
            if price is None or price <= 0:
                rejected["invalid_price"] += 1
                continue
            turnover = self._optional_number(row, ("成交额", "amount", "turnover"))
            if turnover is None or turnover < self.min_snapshot_turnover:
                rejected["below_min_turnover"] += 1
                continue
            trend_field = "60日涨跌幅"
            trend = self._optional_number(row, (trend_field,))
            if trend is None:
                trend_field = "年初至今涨跌幅"
                trend = self._optional_number(row, (trend_field,))
            if trend is None:
                trend_field = "涨跌幅"
                trend = self._optional_number(row, (trend_field,))
            if trend is None:
                trend_field = "neutral_missing"
                trend = 0.0
            candidates.append({"symbol": symbol, "name": name, "turnover": turnover,
                               "trend": trend, "trend_field": trend_field})

        if not candidates:
            return self.symbols, self._configured_universe_audit(
                end, "all_a_snapshot_has_no_eligible_candidates"
            )

        # Cross-sectional liquidity ranks avoid hard-coding a market-size
        # scale; the bounded 60-day trend proxy prevents a one-day spike from
        # dominating.  Symbol is the final tie-breaker for reproducible tests.
        liquidity_order = sorted(candidates, key=lambda item: (float(item["turnover"]), str(item["symbol"])))
        denominator = max(1, len(liquidity_order) - 1)
        liquidity_rank = {
            str(item["symbol"]): index / denominator for index, item in enumerate(liquidity_order)
        }
        for item in candidates:
            trend = max(-50.0, min(100.0, float(item["trend"])))
            trend_score = (trend + 50.0) / 150.0
            item["selection_score"] = round(
                liquidity_rank[str(item["symbol"])] * 0.7 + trend_score * 0.3, 6
            )
        ranked = sorted(
            candidates,
            key=lambda item: (-float(item["selection_score"]), -float(item["turnover"]), str(item["symbol"])),
        )
        selected = ranked[: self.dynamic_universe_limit]
        symbols = tuple(str(item["symbol"]) for item in selected)
        audit = {
            "mode": "dynamic_current_snapshot",
            "source": "AKShare.stock_zh_a_spot_em",
            "requested_as_of": end.isoformat(),
            "selection_snapshot_kind": "runtime_realtime_or_latest_close_snapshot",
            "selection_snapshot_is_pit": False,
            "production_ready": False,
            "simulation_matching_ready": False,
            "fallback_used": False,
            "fallback_reason": None,
            "configured_fallback_count": len(self.symbols),
            "raw_rows": len(records),
            "eligible_rows": len(candidates),
            "rejected": rejected,
            "minimum_turnover_cny": self.min_snapshot_turnover,
            "maximum_candidates": self.dynamic_universe_limit,
            "score_formula": "0.70×成交额横截面分位 + 0.30×[-50%,100%]截断趋势归一分",
            "trend_fallback_order": ["60日涨跌幅", "年初至今涨跌幅", "涨跌幅", "neutral_missing"],
            "selected_count": len(symbols),
            "selected_symbols": list(symbols),
            "selected": [
                {"symbol": item["symbol"], "name": item["name"],
                 "turnover_cny": round(float(item["turnover"]), 2),
                 "trend_percent": round(float(item["trend"]), 4),
                 "trend_field": item["trend_field"], "score": item["selection_score"]}
                for item in selected
            ],
            "warning": "该排名只反映请求时的当前快照，不是历史PIT股票池，不能用于历史成分重建或模拟撮合",
        }
        return symbols, audit

    def status(self) -> dict:
        try: import akshare  # noqa
        except ImportError: return {"provider": self.name, "available": False, "reason": "optional package not installed"}
        return {"provider": self.name, "available": True, "observation_only": True,
                "production_ready": False, "pit_verified": False,
                "metadata_fallback_configured": self.metadata_path is not None,
                "dynamic_universe": self.dynamic_universe,
                "dynamic_universe_limit": self.dynamic_universe_limit,
                "min_snapshot_turnover": self.min_snapshot_turnover,
                "min_listed_days": self.min_listed_days,
                "min_request_interval_seconds": self.min_request_interval_seconds,
                "warning": "前复权行情仅供前瞻观察；网页接口未证明PIT、历史成分或商业授权"}

    def load(self, as_of: date | None = None) -> DataSnapshot:
        status = self.status()
        if not status["available"]:
            raise RuntimeError(status["reason"])
        if not self.symbols: raise RuntimeError("configure prototype symbols explicitly")
        import akshare as ak
        end = as_of or date.today()
        # Roughly one trading year ensures established stocks have enough
        # history for the model's 60/120-session factors and diagnostics.
        start = end - timedelta(days=260)
        bars: list[Bar] = []
        history_counts: dict[str, int] = {}
        configured_metadata = self._configured_security_metadata()
        selected_symbols, universe_selection = self._select_observation_universe(
            ak, end, allow_current_snapshot=as_of is None or as_of == date.today()
        )
        metadata_sources: dict[str, str] = {}
        security_metadata: dict[str, tuple[str, str, date]] = {}
        live_metadata_available = True
        live_metadata_error: str | None = None
        history_sources: dict[str, str] = {}
        eastmoney_history_available = True
        eastmoney_history_error: str | None = None

        if universe_selection["mode"] == "dynamic_current_snapshot":
            dynamic_metadata: dict[str, tuple[str, str, date]] = {}
            for symbol in selected_symbols:
                try:
                    dynamic_metadata[symbol] = self._security_info(ak, symbol)
                    metadata_sources[symbol] = "stock_individual_info_em"
                except RuntimeError as exc:
                    live_metadata_available = False
                    live_metadata_error = str(exc)
                    break
            if not live_metadata_available:
                missing = [symbol for symbol in self.symbols if symbol not in configured_metadata]
                if missing:
                    raise RuntimeError(
                        f"AKShare dynamic industry metadata is unavailable: {live_metadata_error}; "
                        f"configured metadata is missing {missing[0]}; observation is blocked"
                    ) from None
                dynamic_attempt = universe_selection
                universe_selection = self._configured_universe_audit(
                    end, "dynamic_industry_metadata_unavailable", live_metadata_error
                )
                universe_selection["dynamic_attempt"] = dynamic_attempt
                selected_symbols = self.symbols
                security_metadata = {symbol: configured_metadata[symbol] for symbol in selected_symbols}
                metadata_sources = {symbol: "configured_static_fallback" for symbol in selected_symbols}
            else:
                excluded_recent = [
                    symbol for symbol, (_, _, listed_at) in dynamic_metadata.items()
                    if (end - listed_at).days < self.min_listed_days
                ]
                selected_symbols = tuple(symbol for symbol in selected_symbols if symbol not in excluded_recent)
                universe_selection["listing_age_filter"] = {
                    "status": "available_from_stock_individual_info_em",
                    "minimum_calendar_days": self.min_listed_days,
                    "excluded_count": len(excluded_recent),
                    "excluded_symbols": excluded_recent,
                }
                universe_selection["selected_count"] = len(selected_symbols)
                universe_selection["selected_symbols"] = list(selected_symbols)
                if not selected_symbols:
                    missing = [symbol for symbol in self.symbols if symbol not in configured_metadata]
                    if missing:
                        raise RuntimeError(
                            "AKShare dynamic universe has no candidates after listing-age filtering and "
                            f"configured metadata is missing {missing[0]}; observation is blocked"
                        )
                    dynamic_attempt = universe_selection
                    universe_selection = self._configured_universe_audit(
                        end, "no_dynamic_candidates_after_listing_age_filter"
                    )
                    universe_selection["dynamic_attempt"] = dynamic_attempt
                    selected_symbols = self.symbols
                    security_metadata = {symbol: configured_metadata[symbol] for symbol in selected_symbols}
                    metadata_sources = {symbol: "configured_static_fallback" for symbol in selected_symbols}
                else:
                    security_metadata = {symbol: dynamic_metadata[symbol] for symbol in selected_symbols}
                    metadata_sources = {symbol: "stock_individual_info_em" for symbol in selected_symbols}
        else:
            for symbol in selected_symbols:
                if live_metadata_available:
                    try:
                        security_metadata[symbol] = self._security_info(ak, symbol)
                        metadata_sources[symbol] = "stock_individual_info_em"
                    except RuntimeError as exc:
                        live_metadata_available = False
                        live_metadata_error = str(exc)
                if not live_metadata_available:
                    fallback = configured_metadata.get(symbol)
                    if fallback is None:
                        raise RuntimeError(
                            f"AKShare live metadata is unavailable for {symbol}: {live_metadata_error}; "
                            "configured metadata is missing; observation is blocked"
                        ) from None
                    security_metadata[symbol] = fallback
                    metadata_sources[symbol] = "configured_static_fallback"

        for symbol in selected_symbols:
            name, industry, listed_at = security_metadata[symbol]
            if not industry:
                raise RuntimeError(f"AKShare industry is unavailable for {symbol}; observation is blocked")
            dataset = "stock_zh_a_hist"
            volume_multiplier = 100
            if eastmoney_history_available:
                try:
                    records = self._request(
                        ak, dataset, symbol=symbol.split(".", 1)[0], period="daily",
                        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="qfq",
                    )
                    history_sources[symbol] = "eastmoney_qfq"
                except RuntimeError as exc:
                    eastmoney_history_available = False
                    eastmoney_history_error = str(exc)
            if not eastmoney_history_available:
                dataset = "stock_zh_a_daily"
                volume_multiplier = 1
                records = self._request(
                    ak, dataset, symbol=self._sina_symbol(symbol),
                    start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="qfq",
                )
                history_sources[symbol] = "sina_qfq_fallback"
            if not records:
                raise RuntimeError(f"AKShare qfq history is empty for {symbol}; observation is blocked")
            symbol_bars: list[Bar] = []
            seen_days: set[date] = set()
            for row in records:
                day = self._day(self._value(row, ("日期", "date"), dataset=dataset), dataset=dataset)
                if day > end or day in seen_days:
                    raise RuntimeError(f"AKShare qfq history has an invalid or duplicate date for {symbol}; observation is blocked")
                seen_days.add(day)
                try:
                    op = float(self._value(row, ("开盘", "open"), dataset=dataset))
                    close = float(self._value(row, ("收盘", "close"), dataset=dataset))
                    high = float(self._value(row, ("最高", "high"), dataset=dataset))
                    low = float(self._value(row, ("最低", "low"), dataset=dataset))
                    volume = int(float(self._value(row, ("成交量", "volume"), dataset=dataset)) * volume_multiplier)
                    amount = float(self._value(row, ("成交额", "amount"), dataset=dataset))
                except (TypeError, ValueError, OverflowError):
                    raise RuntimeError(f"AKShare qfq history has non-numeric values for {symbol}; observation is blocked") from None
                numeric = (op, close, high, low, float(volume), amount)
                if (not all(math.isfinite(value) for value in numeric) or min(op, close, high, low) <= 0 or
                        volume < 0 or amount < 0 or low > min(op, close) or high < max(op, close)):
                    raise RuntimeError(f"AKShare qfq history has invalid OHLCV for {symbol}; observation is blocked")
                symbol_bars.append(Bar(symbol, name, day, op, high, low, close, volume, amount,
                                       industry, industry, board=TushareProvider._board(None, symbol),
                                       listed_days=max(0, (day - listed_at).days), quality=50.0, catalyst=50.0,
                                       # Prices are already transformed by the provider.  1.0 is
                                       # the identity factor in this transformed series, not a
                                       # claim about the raw corporate-action factor.
                                       adj_factor=1.0))
            symbol_bars.sort(key=lambda bar: bar.day)
            if (end - listed_at).days >= 120 and len(symbol_bars) < 60:
                raise RuntimeError(f"AKShare qfq history is too short for {symbol}; observation is blocked")
            history_counts[symbol] = len(symbol_bars)
            bars.extend(symbol_bars)
        if not bars:
            raise RuntimeError("AKShare returned no qfq bars; observation is blocked")
        latest = max(x.day for x in bars)
        market_inputs, market_enrichment = self._public_market_enrichment(ak, end, bars)
        metadata = {**self.status(), "prototype": True, "observation_only": True, "public_data": True,
                    "research_eligible": False, "production_ready": False,
                    "pit_verified": False, "pit_reconstruction": False, "authorization": False,
                    "simulation_matching_ready": False,
                    "universe_selection": universe_selection,
                    "theme_mapping": {"status": "industry_fallback",
                                      "source": "stock_individual_info_em.行业 + 显式静态元数据回退" if not live_metadata_available else "stock_individual_info_em.行业",
                                      "missing_symbols": 0,
                                      "warning": "当前行业快照仅作题材分组回退，不具备历史PIT成分"},
                    "security_metadata": {"sources": metadata_sources,
                                          "live_endpoint_available": live_metadata_available,
                                          "live_endpoint_error": live_metadata_error,
                                          "configured_file": self.metadata_path.name if self.metadata_path else None,
                                          "minimum_listed_days": self.min_listed_days,
                                          "warning": "当前行业/上市日期只用于运行期预选；静态回退和实时端点均不是PIT历史成分"},
                    "price_history": {"sources": history_sources,
                                      "eastmoney_endpoint_available": eastmoney_history_available,
                                      "eastmoney_endpoint_error": eastmoney_history_error,
                                      "adjustment": "qfq",
                                      "volume_unit": "shares",
                                      "warning": "东方财富失败时切换至AKShare新浪前复权日线；实际来源逐标的审计"},
                    "enrichments": {"adj_factor": {"status": "available", "method": "provider_qfq",
                                                    "rows": len(bars), "missing_bar_rows": 0,
                                                    "warning": "接口直接返回前复权价格，不提供可审计的逐日原始复权因子"},
                                    "daily_basic": {"status": "unavailable", "reason": "not_collected",
                                                    "warning": "估值与流动性增强项按中性降级"},
                                    "index_daily": {"status": market_enrichment["global_risk"]["status"],
                                                   "quality": market_enrichment["global_risk"]["quality"],
                                                   "warning": "公开多资产代理只用于前瞻观察，不具备历史PIT保证"},
                                    "global_risk": market_enrichment["global_risk"],
                                    "fund_flow": market_enrichment["fund_flow"],
                                    "valuation": market_enrichment["valuation"]},
                    "market_inputs": market_inputs,
                    "data_quality": {"status": "observation_ready", "qfq_validated": True,
                                     "industry_complete": True, "history_counts": history_counts},
                    "neutral_stock_fields": {"quality": 50.0, "catalyst": 50.0,
                                             "reason": "公开接口未提供可验证的质量与催化评分"}}
        return DataSnapshot(datetime.combine(latest, time(18), SHANGHAI), bars, self.name, len(selected_symbols), metadata)


def provider_from_env(environ: dict[str, str] | None = None) -> MarketDataProvider:
    """Create the configured provider; never silently fall back after bad config."""
    env = os.environ if environ is None else environ
    kind = env.get("QUANT_DATA_PROVIDER", "demo").strip().lower()
    if kind == "demo":
        return DeterministicDemoProvider()
    if kind == "csv":
        raw = env.get("QUANT_DATA_PATH", "").strip()
        if not raw: raise RuntimeError("QUANT_DATA_PATH is required when QUANT_DATA_PROVIDER=csv")
        path = Path(raw)
        if not path.is_file(): raise RuntimeError(f"configured CSV does not exist: {path}")
        return CsvProvider(path)
    if kind == "licensed-csv":
        raw = env.get("QUANT_DATA_BUNDLE", "").strip()
        if not raw: raise RuntimeError("QUANT_DATA_BUNDLE is required when QUANT_DATA_PROVIDER=licensed-csv")
        provider = LicensedCsvBundleProvider(raw)
        status = provider.status()
        if not status["production_ready"]:
            raise RuntimeError(f"licensed bundle is not production-ready: {status['reason']}; errors={status['manifest_errors']}")
        return provider
    if kind == "tushare":
        token = next((env.get(key, "").strip() for key in TushareProvider.token_environment_keys if env.get(key, "").strip()), "")
        if not token:
            raise RuntimeError("QUANT_TUSHARE_TOKEN (or TUSHARE_TOKEN) is required when QUANT_DATA_PROVIDER=tushare")
        # 1.25 seconds stays below the common 50 calls/minute public tier once
        # trade-calendar and stock-basic requests are included.
        raw_interval = env.get("QUANT_TUSHARE_MIN_REQUEST_INTERVAL_SECONDS", "1.25").strip()
        try:
            interval = float(raw_interval)
        except ValueError as exc:
            raise RuntimeError("QUANT_TUSHARE_MIN_REQUEST_INTERVAL_SECONDS must be a non-negative number") from exc
        if interval < 0:
            raise RuntimeError("QUANT_TUSHARE_MIN_REQUEST_INTERVAL_SECONDS must be a non-negative number")
        return TushareProvider(token, min_request_interval_seconds=interval)
    if kind == "akshare":
        raw_symbols = env.get("QUANT_AKSHARE_SYMBOLS", "")
        symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()))
        if not symbols:
            raise RuntimeError("QUANT_AKSHARE_SYMBOLS is required when QUANT_DATA_PROVIDER=akshare; provide comma-separated A-share symbols")
        raw_interval = env.get("QUANT_AKSHARE_MIN_REQUEST_INTERVAL_SECONDS", "0.5").strip()
        try:
            interval = float(raw_interval)
        except ValueError as exc:
            raise RuntimeError("QUANT_AKSHARE_MIN_REQUEST_INTERVAL_SECONDS must be a non-negative number") from exc
        if interval < 0:
            raise RuntimeError("QUANT_AKSHARE_MIN_REQUEST_INTERVAL_SECONDS must be a non-negative number")
        raw_dynamic = env.get("QUANT_AKSHARE_DYNAMIC_UNIVERSE", "true").strip().lower()
        if raw_dynamic not in {"true", "false", "1", "0", "yes", "no"}:
            raise RuntimeError("QUANT_AKSHARE_DYNAMIC_UNIVERSE must be true or false")
        dynamic_universe = raw_dynamic in {"true", "1", "yes"}
        try:
            dynamic_limit = int(env.get("QUANT_AKSHARE_DYNAMIC_UNIVERSE_LIMIT", "60").strip())
        except ValueError as exc:
            raise RuntimeError("QUANT_AKSHARE_DYNAMIC_UNIVERSE_LIMIT must be an integer between 1 and 60") from exc
        if not 1 <= dynamic_limit <= 60:
            raise RuntimeError("QUANT_AKSHARE_DYNAMIC_UNIVERSE_LIMIT must be an integer between 1 and 60")
        try:
            min_turnover = float(env.get("QUANT_AKSHARE_MIN_SNAPSHOT_TURNOVER", "100000000").strip())
        except ValueError as exc:
            raise RuntimeError("QUANT_AKSHARE_MIN_SNAPSHOT_TURNOVER must be a non-negative finite number") from exc
        if not math.isfinite(min_turnover) or min_turnover < 0:
            raise RuntimeError("QUANT_AKSHARE_MIN_SNAPSHOT_TURNOVER must be a non-negative finite number")
        try:
            min_listed_days = int(env.get("QUANT_AKSHARE_MIN_LISTED_DAYS", "120").strip())
        except ValueError as exc:
            raise RuntimeError("QUANT_AKSHARE_MIN_LISTED_DAYS must be a non-negative integer") from exc
        if min_listed_days < 0:
            raise RuntimeError("QUANT_AKSHARE_MIN_LISTED_DAYS must be a non-negative integer")
        raw_metadata = env.get("QUANT_AKSHARE_METADATA_PATH", "").strip()
        metadata_path = Path(raw_metadata) if raw_metadata else None
        if metadata_path is not None and not metadata_path.is_file():
            raise RuntimeError(f"configured AKShare metadata does not exist: {metadata_path}")
        return AkshareProvider(
            symbols, metadata_path=metadata_path, min_request_interval_seconds=interval,
            dynamic_universe=dynamic_universe, dynamic_universe_limit=dynamic_limit,
            min_snapshot_turnover=min_turnover, min_listed_days=min_listed_days,
        )
    raise RuntimeError(f"unsupported QUANT_DATA_PROVIDER={kind!r}; expected demo, csv, licensed-csv, tushare, or akshare")
