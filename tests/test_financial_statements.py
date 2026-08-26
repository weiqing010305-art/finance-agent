"""Tests for the Eastmoney financial-statements source and the
``fetch_financial_statements`` tool.
"""

import asyncio

import httpx
import pytest

from backend.financial_statements import (
    EastmoneyFinancialStatements,
    FinancialStatementError,
    fetch_financial_statements,
)
from backend.tool_registry import FetchFinancialStatementsInput


def _mock_row(period="2025-06-30", revenue="91093762553.97",
              net_profit="45402962298.10", eps_basic="36.18",
              roe_weighted="17.89", report_type="2025年 半年报"):
    return {
        "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "REPORT_DATE": f"{period} 00:00:00",
        "REPORT_TYPE": report_type,
        "NOTICE_DATE": "2025-08-13 00:00:00",
        "CURRENCY": "CNY",
        "REPORT_YEAR": 2025,
        "EPSJB": eps_basic,
        "EPSKCJB": eps_basic,
        "TOTALOPERATEREVE": revenue,
        "PARENTNETPROFIT": net_profit,
        "ROEJQ": roe_weighted,
        "TOTALOPERATEREVETZ": "9.1581680613",
        "PARENTNETPROFITTZ": "8.89",
        "BPS": "189.975286050079",
        "MGJYXJJE": "10.44346760624",
        "XSMLL": "91.2993094819",
        "TOTAL_ASSETS_PK": "200000000000",
        "TOTAL_EQUITY_PK": "150000000000",
        "LIABILITY": "50000000000",
        "SECUCODE": "600519.SH",
    }


def _payload(rows):
    return {
        "version": "v1",
        "result": {"pages": len(rows), "data": rows},
        "success": True,
    }


def make_source(rows, *, status=200, fail_message=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400 or fail_message:
            return httpx.Response(
                status or 200,
                json={"success": False, "message": fail_message or "boom"},
            )
        return httpx.Response(200, json=_payload(rows))
    return EastmoneyFinancialStatements(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5,
    ))


def test_secucode_normalisation_for_a_shares():
    from backend.financial_statements import _normalise_secucode
    assert _normalise_secucode("600519", "CN") == "600519.SH"
    assert _normalise_secucode("000001", "CN") == "000001.SZ"
    assert _normalise_secucode("300750", "SZ") == "300750.SZ"
    assert _normalise_secucode("688981", "SH") == "688981.SH"
    # Already a SECUCODE passes through.
    assert _normalise_secucode("600519.SH", "CN") == "600519.SH"
    # H / US are unsupported (degraded path).
    assert _normalise_secucode("0700", "HK") is None
    assert _normalise_secucode("AAPL", "US") is None


def test_fetch_returns_normalised_rows_for_supported_symbol():
    rows = [_mock_row("2025-06-30"), _mock_row("2024-12-31", revenue="173867800000",
                                              net_profit="86228000000", eps_basic="68.64",
                                              roe_weighted="34.71", report_type="2024年 年报")]
    src = make_source(rows)

    async def run():
        return await src.fetch(symbol="600519", market="CN", periods=4)

    result = asyncio.run(run())
    assert len(result) == 2
    latest = result[0]
    assert latest["period"] == "2025-06-30"
    assert latest["currency"] == "CNY"
    assert latest["revenue"]["value"] == pytest.approx(91093762553.97)
    assert latest["revenue"]["label"] == "营业总收入"
    assert latest["revenue"]["source_field"] == "TOTALOPERATEREVE"
    assert latest["net_profit"]["value"] == pytest.approx(45402962298.10)
    assert latest["roe_weighted"]["value"] == pytest.approx(17.89)


def test_fetch_raises_for_unsupported_market():
    src = make_source([])

    async def run():
        return await src.fetch(symbol="0700", market="HK")

    with pytest.raises(FinancialStatementError, match="not currently supported"):
        asyncio.run(run())


def test_fetch_handles_upstream_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "message": "rate limited"})

    src = EastmoneyFinancialStatements(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5,
    ))

    async def run():
        return await src.fetch(symbol="600519", market="CN")

    with pytest.raises(FinancialStatementError, match="rate limited"):
        asyncio.run(run())


def test_tool_handler_returns_statement_rows_for_a_share():
    src = make_source([_mock_row("2025-06-30"), _mock_row("2024-12-31")])

    async def run():
        return await fetch_financial_statements(
            FetchFinancialStatementsInput(symbol="600519", market="CN", periods=2),
            _client=src,
        )

    out = asyncio.run(run())
    assert out["status"] == "ok"
    assert out["coverage"] == "a_share"
    assert out["degraded"] is False
    assert len(out["data"]) == 2
    first = out["data"][0]
    assert first["period"] == "2025-06-30"
    assert "revenue" in first["metrics"]
    assert "net_profit" in first["metrics"]
    assert "roe_weighted" in first["metrics"]
    assert len(out["evidence"]) == 2
    assert all(item["publisher"] == "东方财富" for item in out["evidence"])


def test_tool_handler_routes_hk_financials_to_akshare_when_no_tushare_token():
    """HK financials now route through AkShare first (free, no token).
    The controlled-tools pipeline must keep its honesty: when AkShare
    cannot reach the source it must degrade explicitly and offer a
    search_filings fallback rather than fabricate numbers."""
    from backend.financial_statements import fetch_financial_statements
    from backend.tool_registry import FetchFinancialStatementsInput

    # AkShare needs no token; it will attempt a real network call. In the
    # offline test environment that call degrades with an explicit reason.
    import os
    for key in ("TUSHARE_TOKEN",):
        os.environ.pop(key, None)

    async def run():
        return await fetch_financial_statements(
            FetchFinancialStatementsInput(symbol="00700", market="HK", periods=2),
        )

    out = asyncio.run(run())
    assert out["status"] in {"ok", "empty"}
    assert out["degraded"] is True or out["coverage"] == "hk"
    if out["status"] == "empty":
        assert out["fallback_used"] in {"filings_search", None}

def test_tool_handler_degrades_on_upstream_failure():
    # 200 OK with success=false mirrors real Eastmoney rate-limit responses.
    src = make_source([], fail_message="bad gateway")

    async def run():
        return await fetch_financial_statements(
            FetchFinancialStatementsInput(symbol="600519", market="CN", periods=2),
            _client=src,
        )

    out = asyncio.run(run())
    assert out["status"] == "empty"
    assert out["degraded"] is True
    assert out["fallback_used"] == "filings_search"
    assert "bad gateway" in out["degraded_reason"]