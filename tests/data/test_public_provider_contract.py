"""Contract boundaries for prototype public market-data providers.

These tests deliberately do not call a public endpoint.  Availability of a
third-party web API is not a deterministic test fixture; provider selection,
failure behaviour and production claims are.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from quant_system.providers import (
    AkshareProvider,
    DeterministicDemoProvider,
    TushareProvider,
    provider_from_env,
)


def _optional_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make the optional import deterministic without invoking a network SDK."""
    monkeypatch.setitem(sys.modules, name, ModuleType(name))


def test_tushare_factory_requires_explicit_token_and_never_returns_demo(monkeypatch):
    _optional_module(monkeypatch, "tushare")

    with pytest.raises(RuntimeError, match="TUSHARE_TOKEN"):
        provider_from_env({"QUANT_DATA_PROVIDER": "tushare"})

    secret = "ts_test_token_must_not_escape_7f34"
    provider = provider_from_env(
        {"QUANT_DATA_PROVIDER": "tushare", "QUANT_TUSHARE_TOKEN": secret}
    )

    assert isinstance(provider, TushareProvider)
    assert not isinstance(provider, DeterministicDemoProvider)
    assert provider.token == secret


def test_tushare_factory_accepts_legacy_token_name_without_leaking_it(monkeypatch):
    _optional_module(monkeypatch, "tushare")
    secret = "ts_legacy_token_must_not_escape_7f34"
    provider = provider_from_env(
        {"QUANT_DATA_PROVIDER": "tushare", "TUSHARE_TOKEN": secret}
    )

    assert isinstance(provider, TushareProvider)
    assert secret not in json.dumps(provider.status(), ensure_ascii=False)


def test_akshare_factory_requires_explicit_symbols_and_never_returns_demo(monkeypatch):
    _optional_module(monkeypatch, "akshare")

    with pytest.raises(RuntimeError, match="QUANT_AKSHARE_SYMBOLS"):
        provider_from_env({"QUANT_DATA_PROVIDER": "akshare"})

    provider = provider_from_env(
        {
            "QUANT_DATA_PROVIDER": "akshare",
            "QUANT_AKSHARE_SYMBOLS": "600519.SH, 000001.SZ,600519.SH",
        }
    )

    assert isinstance(provider, AkshareProvider)
    assert not isinstance(provider, DeterministicDemoProvider)
    assert provider.symbols == ("600519.SH", "000001.SZ")


@pytest.mark.parametrize(
    "configured_kind, environment",
    [
        ("tushare", {"QUANT_DATA_PROVIDER": "tushare"}),
        ("akshare", {"QUANT_DATA_PROVIDER": "akshare"}),
    ],
)
def test_bad_public_provider_configuration_is_fail_closed_not_demo(configured_kind, environment):
    with pytest.raises(RuntimeError):
        provider_from_env(environment)

    # The only implicit fallback is an entirely absent provider selection.
    assert isinstance(provider_from_env({}), DeterministicDemoProvider)
    assert configured_kind in {"tushare", "akshare"}


def test_tushare_status_is_prototype_only_pit_unverified_and_token_safe(monkeypatch):
    _optional_module(monkeypatch, "tushare")
    secret = "ts_test_token_must_not_escape_7f34"

    absent = TushareProvider().status()
    status = TushareProvider(secret).status()
    serialized = json.dumps(status, ensure_ascii=False, sort_keys=True)

    assert absent["available"] is False
    assert absent["production_ready"] is False
    assert absent["pit_verified"] is False
    assert "token" in absent["reason"].lower()
    assert status["provider"] == "tushare"
    assert status["available"] is True
    assert status["production_ready"] is False
    assert status["pit_verified"] is False
    assert "PIT" in status["warning"]
    assert secret not in serialized
    assert secret not in repr(TushareProvider(secret))


def test_tushare_request_errors_do_not_surface_or_chain_a_provider_token():
    secret = "ts_provider_error_token_must_not_escape_7f34"

    class LeakingSdkClient:
        def daily(self, **_kwargs):
            raise RuntimeError(f"remote rejected credential {secret}")

    with pytest.raises(RuntimeError) as raised:
        TushareProvider(secret)._request(LeakingSdkClient(), "daily", trade_date="20260703")

    # Error text is sent to the API boundary and the chained exception is
    # normally rendered by structured/unhandled-error loggers as well.
    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None
