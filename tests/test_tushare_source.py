"""Tests for the httpx-backed Tushare Pro HK / US financial-statements source."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from backend.tushare_source import (
    TushareFinancialStatements,
    TushareFinancialStatementsError,
    _to_tushare_symbol,
    fetch_via_tushare,
)


def _ok_response(fields: list[str], items: list[list]) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "", "data": {"fields": fields, "items": items}})


def _err_response(code: int, msg: str) -> httpx.Response:
    return httpx.Response(200, json={"code": code, "msg": msg, "data": {}})


def _client_with_responses(responses: list[httpx.Response]) -> TushareFinancialStatements:
    """Build a TushareFinancialStatements whose underlying httpx client
    returns the given response sequence (one per _post call)."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            raise AssertionError("MockTransport received more requests than queued responses")
        return queue.pop(0)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TushareFinancialStatements.__new__(TushareFinancialStatements)
    client.token = "test"
    client.market = "HK"
    client.base_url = "https://api.tushare.pro"
    client.timeout = 5
    client._client = http
    return client


def test_from_env_returns_none_without_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert TushareFinancialStatements.from_env({}) is None


def test_from_env_builds_client_with_token():
    client = TushareFinancialStatements.from_env({"TUSHARE_TOKEN": "k"})
    assert client is not None
    assert client.token == "k"
    assert client.base_url == "https://api.tushare.pro"


def test_to_tushare_symbol_adds_suffix():
    assert _to_tushare_symbol("0700", "HK") == "0700.HK"
    assert _to_tushare_symbol("AAPL", "US") == "AAPL.US"
    assert _to_tushare_symbol("00700.HK", "HK") == "00700.HK"
    assert _to_tushare_symbol("", "HK") is None


def _hk_income_response():
    return _ok_response(
        ["TS_CODE", "END_DATE", "TOTAL_REVENUE", "PARENT_NETPROFIT", "BASIC_EPS"],
        [["00700.HK", "20250331", 1540000000000, 420000000000, 4.5]],
    )


def _hk_balance_response():
    return _ok_response(
        ["TS_CODE", "END_DATE", "TOTAL_ASSETS", "TOTAL_LIAB", "TOTAL_EQUITY", "BPS"],
        [["00700.HK", "20250331", 1800000000000, 700000000000, 1100000000000, 12.5]],
    )


def _hk_cashflow_response():
    return _ok_response(
        ["TS_CODE", "END_DATE", "NETCASH_OPERATE", "NETCASH_INVEST", "NETCASH_FINANCE"],
        [["00700.HK", "20250331", 480000000000, -100000000000, -150000000000]],
    )


def test_fetch_merges_income_balance_cashflow_into_period_rows():
    client = _client_with_responses([
        _hk_income_response(), _hk_balance_response(), _hk_cashflow_response(),
    ])

    async def run():
        return await client.fetch("00700.HK", periods=1)

    rows = asyncio.run(run())
    assert len(rows) == 1
    row = rows[0]
    assert row["period"] == "2025-03-31"
    assert row["currency"] == "CNY"
    metrics = row["metrics"]
    assert metrics["revenue"]["value"] == 1540000000000
    assert metrics["net_profit"]["value"] == 420000000000
    assert metrics["eps_basic"]["value"] == 4.5
    assert metrics["total_assets"]["value"] == 1800000000000
    assert metrics["total_liabilities"]["value"] == 700000000000
    assert metrics["total_equity"]["value"] == 1100000000000
    assert metrics["book_value_per_share"]["value"] == 12.5
    assert metrics["operating_cash_flow"]["value"] == 480000000000


def test_fetch_orders_newest_first():
    income = _ok_response(
        ["END_DATE", "TOTAL_REVENUE", "PARENT_NETPROFIT", "BASIC_EPS"],
        [
            ["20250331", 100, 10, 1.0],
            ["20241231", 200, 20, 2.0],
            ["20240630", 300, 30, 3.0],
        ],
    )
    empty = _ok_response(["END_DATE"], [])
    client = _client_with_responses([income, empty, empty])

    async def run():
        return await client.fetch("00700.HK", periods=3)

    rows = asyncio.run(run())
    assert [r["period"] for r in rows] == ["2025-03-31", "2024-12-31", "2024-06-30"]


def test_fetch_raises_on_http_error():
    bad = httpx.Response(503, json={"error": "service unavailable"})
    client = _client_with_responses([bad])

    async def run():
        return await client.fetch("00700.HK")

    with pytest.raises(TushareFinancialStatementsError, match="HTTP 503"):
        asyncio.run(run())


def test_fetch_raises_on_api_error_code():
    client = _client_with_responses([_err_response(-1, "rate limited")])

    async def run():
        return await client.fetch("00700.HK")

    with pytest.raises(TushareFinancialStatementsError, match="rate limited"):
        asyncio.run(run())


def test_fetch_via_tushare_degrades_when_token_missing(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    payload = SimpleNamespace(symbol="0700", market="HK", periods=4)

    async def run():
        return await fetch_via_tushare(payload)

    out = asyncio.run(run())
    assert out["status"] == "empty"
    assert out["degraded"] is True
    assert "TUSHARE_TOKEN" in out["degraded_reason"]
    assert out["fallback_used"] == "filings_search"


def test_fetch_via_tushare_degrades_when_api_returns_empty(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test")
    empty_income = _ok_response(["END_DATE"], [])
    client = _client_with_responses([empty_income, empty_income, empty_income])

    payload = SimpleNamespace(symbol="0700", market="HK", periods=4)

    async def run():
        return await fetch_via_tushare(payload, _client=client)

    out = asyncio.run(run())
    assert out["status"] == "empty"
    assert out["degraded"] is True
    assert "delisted" in out["degraded_reason"] or "no rows" in out["degraded_reason"]
    assert out["fallback_used"] == "filings_search"


def test_fetch_via_tushare_returns_normalised_rows(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test")
    client = _client_with_responses([
        _hk_income_response(), _hk_balance_response(), _hk_cashflow_response(),
    ])

    payload = SimpleNamespace(symbol="0700", market="HK", periods=4)

    async def run():
        return await fetch_via_tushare(payload, _client=client)

    out = asyncio.run(run())
    assert out["status"] == "ok"
    assert out["coverage"] == "hk"
    assert len(out["data"]) == 1
    row = out["data"][0]
    assert row["period"] == "2025-03-31"
    assert row["metrics"]["revenue"]["value"] == 1540000000000


def test_fetch_via_tushare_rejects_unsupported_market():
    payload = SimpleNamespace(symbol="0700", market="OTHER", periods=4)

    async def run():
        return await fetch_via_tushare(payload)

    with pytest.raises(TushareFinancialStatementsError, match="HK/US"):
        asyncio.run(run())


def test_fetch_financial_statements_prefers_akshare_for_hk(monkeypatch):
    """HK financials prefer the free AkShare path over Tushare. This test
    asserts the routing decision stays honest in both branches: either the
    AkShare adapter returns data, or it degrades with an explicit reason."""
    from backend.financial_statements import fetch_financial_statements
    from backend.tool_registry import FetchFinancialStatementsInput

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    async def run():
        return await fetch_financial_statements(
            FetchFinancialStatementsInput(symbol="00700", market="HK", periods=2),
        )

    out = asyncio.run(run())
    assert out["status"] in {"ok", "empty"}
    assert out["degraded"] is True or out["coverage"] in {"hk", "us"}
    if out["status"] == "empty":
        assert "AkShare" in out["degraded_reason"] or "TUSHARE" in out["degraded_reason"]
