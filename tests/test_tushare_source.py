"""Tests for the Tushare Pro HK / US financial-statements source."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.tushare_source import (
    TushareFinancialStatements,
    TushareFinancialStatementsError,
    _to_tushare_symbol,
    fetch_via_tushare,
)


def _df(rows):
    """Return a MagicMock quacking like a Tushare DataFrame."""
    df = MagicMock()
    df.to_dict.return_value = rows
    df.__len__.return_value = len(rows)
    return df


def _client_with_rows(income=None, balance=None, cashflow=None):
    """Build a TushareFinancialStatements whose 3 pro methods return given rows."""
    client = TushareFinancialStatements.__new__(TushareFinancialStatements)
    client.token = "test"
    client.market = "HK"
    client.timeout = 5
    client._pro = MagicMock()
    if income is not None:
        client._pro.stock_hk_income.return_value = _df(income)
    if balance is not None:
        client._pro.stock_hk_balance.return_value = _df(balance)
    if cashflow is not None:
        client._pro.stock_hk_cashflow.return_value = _df(cashflow)
    return client


def test_from_env_returns_none_without_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert TushareFinancialStatements.from_env({}) is None


def test_from_env_builds_client_with_token():
    client = TushareFinancialStatements.from_env({"TUSHARE_TOKEN": "k"})
    assert client is not None
    assert client.token == "k"


def test_to_tushare_symbol_adds_suffix():
    assert _to_tushare_symbol("0700", "HK") == "0700.HK"
    assert _to_tushare_symbol("AAPL", "US") == "AAPL.US"
    assert _to_tushare_symbol("00700.HK", "HK") == "00700.HK"
    assert _to_tushare_symbol("", "HK") is None


def test_fetch_merges_income_balance_cashflow_into_period_rows():
    income = [{
        "TS_CODE": "00700.HK", "END_DATE": "20250331",
        "TOTAL_REVENUE": 1540000000000, "OPERATE_PROFIT": 560000000000,
        "PARENT_NETPROFIT": 420000000000, "BASIC_EPS": 4.5,
    }]
    balance = [{
        "TS_CODE": "00700.HK", "END_DATE": "20250331",
        "TOTAL_ASSETS": 1800000000000, "TOTAL_LIAB": 700000000000,
        "TOTAL_EQUITY": 1100000000000, "BPS": 12.5,
    }]
    cashflow = [{
        "TS_CODE": "00700.HK", "END_DATE": "20250331",
        "NETCASH_OPERATE": 480000000000, "NETCASH_INVEST": -100000000000,
        "NETCASH_FINANCE": -150000000000,
    }]
    client = _client_with_rows(income=income, balance=balance, cashflow=cashflow)

    async def run():
        return await client.fetch("00700.HK", periods=1)

    rows = asyncio.run(run())
    assert len(rows) == 1
    row = rows[0]
    assert row["period"] == "2025-03-31"
    assert row["currency"] == "CNY"
    metrics = row["metrics"]
    # income
    assert metrics["revenue"]["value"] == 1540000000000
    assert metrics["net_profit"]["value"] == 420000000000
    assert metrics["eps_basic"]["value"] == 4.5
    # balance
    assert metrics["total_assets"]["value"] == 1800000000000
    assert metrics["total_liabilities"]["value"] == 700000000000
    assert metrics["total_equity"]["value"] == 1100000000000
    assert metrics["book_value_per_share"]["value"] == 12.5
    # cashflow
    assert metrics["operating_cash_flow"]["value"] == 480000000000


def test_fetch_orders_newest_first_and_skips_missing_periods():
    income = [
        {"END_DATE": "20250331", "TOTAL_REVENUE": 100, "PARENT_NETPROFIT": 10, "BASIC_EPS": 1.0},
        {"END_DATE": "20241231", "TOTAL_REVENUE": 200, "PARENT_NETPROFIT": 20, "BASIC_EPS": 2.0},
        {"END_DATE": "20240630", "TOTAL_REVENUE": 300, "PARENT_NETPROFIT": 30, "BASIC_EPS": 3.0},
    ]
    client = _client_with_rows(income=income, balance=[], cashflow=[])

    async def run():
        return await client.fetch("00700.HK", periods=3)

    rows = asyncio.run(run())
    assert [r["period"] for r in rows] == ["2025-03-31", "2024-12-31", "2024-06-30"]


def test_fetch_raises_when_api_method_raises():
    client = TushareFinancialStatements.__new__(TushareFinancialStatements)
    client.token = "test"
    client.market = "HK"
    client.timeout = 5
    client._pro = MagicMock()
    client._pro.stock_hk_income.side_effect = RuntimeError("network down")

    async def run():
        return await client.fetch("00700.HK")

    with pytest.raises(TushareFinancialStatementsError, match="network down"):
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
    client = _client_with_rows(income=[], balance=[], cashflow=[])

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
    income = [{
        "END_DATE": "20250331", "TOTAL_REVENUE": 1000, "PARENT_NETPROFIT": 100, "BASIC_EPS": 1,
    }]
    client = _client_with_rows(income=income, balance=[], cashflow=[])

    payload = SimpleNamespace(symbol="0700", market="HK", periods=4)

    async def run():
        return await fetch_via_tushare(payload, _client=client)

    out = asyncio.run(run())
    assert out["status"] == "ok"
    assert out["coverage"] == "hk"
    assert len(out["data"]) == 1
    row = out["data"][0]
    assert row["period"] == "2025-03-31"
    assert row["metrics"]["revenue"]["value"] == 1000


def test_fetch_via_tushare_rejects_unsupported_market():
    payload = SimpleNamespace(symbol="0700", market="OTHER", periods=4)

    async def run():
        return await fetch_via_tushare(payload)

    with pytest.raises(TushareFinancialStatementsError, match="HK/US"):
        asyncio.run(run())


def test_fetch_financial_statements_routes_hk_to_tushare(monkeypatch):
    """Integration: A-share tool handler dispatches HK symbols to the
    Tushare adapter. With a token the adapter runs; without one it
    surfaces the explicit TUSHARE_TOKEN degraded reason (never fabricates
    numbers)."""
    from backend.financial_statements import fetch_financial_statements
    from backend.tool_registry import FetchFinancialStatementsInput

    # Path A: no TUSHARE_TOKEN -> Tushare adapter degrades with the
    # explicit missing-token reason.
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    async def run_without_token():
        return await fetch_financial_statements(
            FetchFinancialStatementsInput(symbol="0700", market="HK", periods=4),
        )

    out = asyncio.run(run_without_token())
    assert out["status"] == "empty"
    assert out["degraded"] is True
    assert "TUSHARE_TOKEN" in out["degraded_reason"]
    assert out["fallback_used"] == "filings_search"

    # Path B: a configured TUSHARE_TOKEN lets from_env build a client.
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    from backend.tushare_source import TushareFinancialStatements
    client = TushareFinancialStatements.from_env()
    assert client is not None
    assert client.token == "test-token"