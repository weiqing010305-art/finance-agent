"""Tests for the AkShare-backed stock price source."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.akshare_source import (
    AkShareQuoteSource,
    AkShareSourceError,
    _evidence_url,
    _normalise_symbol,
    _security_prefix,
    fetch_stock_prices,
)


def _bars():
    return [
        {"date": "2026-08-24", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000},
        {"date": "2026-08-25", "open": 10.2, "high": 10.9, "low": 10.0, "close": 10.8, "volume": 1200},
        {"date": "2026-08-26", "open": 10.8, "high": 11.2, "low": 10.6, "close": 11.0, "volume": 1500},
    ]


def _source(caller=None):
    return AkShareQuoteSource(market="CN", periods=30, caller=caller or (lambda symbol: _bars()))


def test_normalise_symbol_strips_suffix():
    assert _normalise_symbol("600519.SH", "CN") == "600519"
    assert _normalise_symbol("00700.HK", "HK") == "00700"
    assert _normalise_symbol("AAPL.US", "US") == "AAPL"
    assert _normalise_symbol("600519", "CN") == "600519"


def test_security_prefix():
    assert _security_prefix("600519", "CN") == "sh"
    assert _security_prefix("000001", "CN") == "sz"
    assert _security_prefix("300750", "CN") == "sz"
    assert _security_prefix("688981", "CN") == "sh"
    assert _security_prefix("830799", "CN") == "bj"
    assert _security_prefix("00700", "HK") == ""


def test_evidence_url_contains_public_page():
    assert "sh600519" in _evidence_url("600519", "CN")
    assert "00700" in _evidence_url("00700", "HK")
    assert "AAPL" in _evidence_url("AAPL", "US")


def test_fetch_returns_newest_first_with_period_limit():
    src = _source()
    rows = asyncio.run(src.fetch("600519"))
    assert rows[0]["date"] == "2026-08-26"
    assert rows[-1]["date"] == "2026-08-24"
    assert len(rows) == 3


def test_fetch_limits_to_requested_periods():
    src = AkShareQuoteSource(market="CN", periods=2, caller=lambda symbol: _bars())
    rows = asyncio.run(src.fetch("600519"))
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-08-26"


def test_fetch_stock_prices_returns_ok_with_quote():
    async def run():
        return await fetch_stock_prices(
            SimpleNamespace(symbol="600519", market="CN", periods=30),
            _source=_source(),
        )

    out = asyncio.run(run())
    assert out["status"] == "ok"
    assert out["coverage"] == "akshare"
    assert out["degraded"] is False
    assert len(out["data"]) == 3
    quote = out["quote"]
    assert quote["latest_close"] == 11.0
    assert quote["latest_date"] == "2026-08-26"
    assert quote["change"] == pytest.approx(0.2)
    assert quote["change_pct"] == pytest.approx(1.8519, abs=0.01)
    assert quote["window_high"] == 11.2
    assert quote["window_low"] == 9.8
    assert len(out["evidence"]) == 1


def test_fetch_stock_prices_degrades_on_source_error():
    def boom(symbol):
        raise AkShareSourceError("network down")

    async def run():
        return await fetch_stock_prices(
            SimpleNamespace(symbol="600519", market="CN", periods=30),
            _source=_source(caller=boom),
        )

    out = asyncio.run(run())
    assert out["status"] == "empty"
    assert out["degraded"] is True
    assert "network down" in out["degraded_reason"]
    assert out["fallback_used"] == "get_quote"


def test_fetch_stock_prices_degrades_on_unsupported_market():
    async def run():
        return await fetch_stock_prices(
            SimpleNamespace(symbol="600519", market="OTHER", periods=30),
        )

    out = asyncio.run(run())
    assert out["status"] == "empty"
    assert out["degraded"] is True
    assert "not supported" in out["degraded_reason"]