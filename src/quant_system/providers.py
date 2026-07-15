from __future__ import annotations

import csv
import json
import math
import os
import time as clock_time
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median
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
                 sleeper=clock_time.sleep, clock=clock_time.monotonic):
        if min_request_interval_seconds < 0:
            raise ValueError("AKShare minimum request interval cannot be negative")
        self.symbols = symbols
        self.metadata_path = Path(metadata_path) if metadata_path else None
        self.min_request_interval_seconds = min_request_interval_seconds
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

    def status(self) -> dict:
        try: import akshare  # noqa
        except ImportError: return {"provider": self.name, "available": False, "reason": "optional package not installed"}
        return {"provider": self.name, "available": True, "observation_only": True,
                "production_ready": False, "pit_verified": False,
                "metadata_fallback_configured": self.metadata_path is not None,
                "min_request_interval_seconds": self.min_request_interval_seconds,
                "warning": "前复权行情仅供前瞻观察；网页接口未证明PIT、历史成分或商业授权"}

    def load(self, as_of: date | None = None) -> DataSnapshot:
        if not self.status()["available"]: raise RuntimeError(self.status()["reason"])
        if not self.symbols: raise RuntimeError("configure prototype symbols explicitly")
        import akshare as ak
        end = as_of or date.today()
        # Roughly one trading year ensures established stocks have enough
        # history for the model's 60/120-session factors and diagnostics.
        start = end - timedelta(days=260)
        bars: list[Bar] = []
        history_counts: dict[str, int] = {}
        configured_metadata = self._configured_security_metadata()
        metadata_sources: dict[str, str] = {}
        live_metadata_available = True
        live_metadata_error: str | None = None
        history_sources: dict[str, str] = {}
        eastmoney_history_available = True
        eastmoney_history_error: str | None = None
        for symbol in self.symbols:
            if live_metadata_available:
                try:
                    name, industry, listed_at = self._security_info(ak, symbol)
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
                name, industry, listed_at = fallback
                metadata_sources[symbol] = "configured_static_fallback"
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
        metadata = {**self.status(), "prototype": True, "observation_only": True, "public_data": True,
                    "research_eligible": False, "production_ready": False,
                    "pit_verified": False, "pit_reconstruction": False, "authorization": False,
                    "simulation_matching_ready": False,
                    "theme_mapping": {"status": "industry_fallback",
                                      "source": "stock_individual_info_em.行业 + 显式静态元数据回退" if not live_metadata_available else "stock_individual_info_em.行业",
                                      "missing_symbols": 0,
                                      "warning": "当前行业快照仅作题材分组回退，不具备历史PIT成分"},
                    "security_metadata": {"sources": metadata_sources,
                                          "live_endpoint_available": live_metadata_available,
                                          "live_endpoint_error": live_metadata_error,
                                          "configured_file": self.metadata_path.name if self.metadata_path else None,
                                          "warning": "静态元数据仅用于显式观察池身份/行业回退，不是PIT历史成分"},
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
                                    "index_daily": {"status": "unavailable", "reason": "not_collected",
                                                   "warning": "指数/全球风险增强项按中性降级"}},
                    "market_inputs": {"global_risk_score": 50.0, "global_risk_quality": "neutral_missing",
                                      "fund_flow_score": 50.0, "fund_flow_quality": "neutral_missing",
                                      "valuation_score": 50.0, "valuation_quality": "neutral_missing",
                                      "source": "AKShare前复权观察源；未采集增强项时显式中性"},
                    "data_quality": {"status": "observation_ready", "qfq_validated": True,
                                     "industry_complete": True, "history_counts": history_counts},
                    "neutral_stock_fields": {"quality": 50.0, "catalyst": 50.0,
                                             "reason": "公开接口未提供可验证的质量与催化评分"}}
        return DataSnapshot(datetime.combine(latest, time(18), SHANGHAI), bars, self.name, len(self.symbols), metadata)


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
        raw_metadata = env.get("QUANT_AKSHARE_METADATA_PATH", "").strip()
        metadata_path = Path(raw_metadata) if raw_metadata else None
        if metadata_path is not None and not metadata_path.is_file():
            raise RuntimeError(f"configured AKShare metadata does not exist: {metadata_path}")
        return AkshareProvider(symbols, metadata_path=metadata_path, min_request_interval_seconds=interval)
    raise RuntimeError(f"unsupported QUANT_DATA_PROVIDER={kind!r}; expected demo, csv, licensed-csv, tushare, or akshare")
