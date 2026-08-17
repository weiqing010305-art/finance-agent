"""Tests for the Tencent quote source and the get_quote tool."""

import asyncio

import httpx
import pytest

from backend.quote_source import (
    QuoteSourceError,
    TencentQuoteSource,
    get_quote,
    normalize_symbol,
)
from backend.tool_registry import GetQuoteInput


def make_payload(fields):
    return 'v_sz000001="' + "~".join(str(field) for field in fields) + '";'


def make_source(fields, *, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/q=")
        if status >= 400:
            return httpx.Response(status, text="boom")
        body = make_payload(fields).encode("gbk")
        return httpx.Response(200, content=body)

    return TencentQuoteSource(transport=httpx.MockTransport(handler))


def quote_fields(**overrides):
    fields = [str(i) for i in range(50)]
    values = {
        "name": "平安银行", "code": "000001", "price": "11.10", "prev_close": "11.11",
        "open": "11.20", "volume": "929043", "time": "20260817161403",
        "change": "-0.01", "change_pct": "-0.09", "high": "11.22", "low": "11.07",
        "turnover": "103195", "turnover_rate": "0.48", "pe": "4.96",
        "amplitude": "1.35", "float_market_cap": "2154.03", "total_market_cap": "2154.06",
        "pb": "0.46",
    }
    values.update(overrides)
    for key, value in values.items():
        fields[{"name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
                "volume": 6, "time": 30, "change": 31, "change_pct": 32,
                "high": 33, "low": 34, "turnover": 37, "turnover_rate": 38,
                "pe": 39, "amplitude": 43, "float_market_cap": 44,
                "total_market_cap": 45, "pb": 46}[key]] = value
    return fields


def run_quote(source, payload):
    return asyncio.run(get_quote(
        GetQuoteInput.model_validate(payload), _source=source
    ))


def test_normalize_symbol_mappings():
    assert normalize_symbol("000001", "CN_A") == "sz000001"
    assert normalize_symbol("600519", "CN_A") == "sh600519"
    assert normalize_symbol("000001", "SZSE") == "sz000001"
    assert normalize_symbol("0700.HK", "HK") == "hk00700"
    assert normalize_symbol("700", "HK") == "hk00700"
    assert normalize_symbol("AAPL", "US") == "usAAPL"
    assert normalize_symbol("sz000001") == "sz000001"
    assert normalize_symbol("hk00700") == "hk00700"
    assert normalize_symbol("", "CN") is None
    assert normalize_symbol("abc", "") is None


def test_parse_a_share_quote():
    source = make_source(quote_fields())
    quote = asyncio.run(source.fetch("000001", "CN"))
    assert quote["symbol"] == "sz000001"
    assert quote["name"] == "平安银行"
    assert quote["price"] == 11.10
    assert quote["change"] == -0.01
    assert quote["change_pct"] == -0.09
    assert quote["high"] == 11.22
    assert quote["low"] == 11.07
    assert quote["prev_close"] == 11.11
    assert quote["volume"] == 929043
    assert quote["pe"] == 4.96
    assert quote["pb"] == 0.46
    assert quote["total_market_cap"] == 2154.06
    assert quote["time"] == "2026-08-17 16:14:03"
    assert quote["source"] == "tencent"


def test_parse_hk_time_format():
    fields = quote_fields(time="2026/08/17 16:08:35")
    quote = asyncio.run(make_source(fields).fetch("00700", "HK"))
    assert quote["time"] == "2026-08-17 16:08:35"


def test_empty_numeric_fields_become_none():
    fields = quote_fields(price="-", pe="", turnover_rate="--")
    quote = asyncio.run(make_source(fields).fetch("000001"))
    assert quote["price"] is None
    assert quote["pe"] is None
    assert quote["turnover_rate"] is None


def test_http_error_raises():
    source = make_source(quote_fields(), status=502)
    with pytest.raises(QuoteSourceError, match="HTTP 502"):
        asyncio.run(source.fetch("000001"))


def test_missing_payload_raises():
    def handler(request):
        return httpx.Response(200, content=b"not a quote")

    source = TencentQuoteSource(transport=httpx.MockTransport(handler))
    with pytest.raises(QuoteSourceError, match="missing the payload"):
        asyncio.run(source.fetch("000001"))


def test_get_quote_handler_returns_deterministic_item():
    source = make_source(quote_fields())
    output = run_quote(source, {"symbol": "000001", "market": "CN"})
    assert output["status"] == "ok"
    assert not output["degraded"]
    item = output["data"][0]
    assert item["symbol"] == "sz000001"
    assert item["price"] == 11.10
    assert item["change_pct"] == -0.09
    assert item["source"] == "tencent"


def test_get_quote_handler_invalid_symbol_degrades():
    source = make_source(quote_fields())
    output = run_quote(source, {"symbol": "???", "market": ""})
    assert output["status"] == "empty"
    assert output["degraded"] is True
    assert "cannot map symbol" in output["degraded_reason"]


def test_get_quote_handler_http_error_degrades():
    source = make_source(quote_fields(), status=500)
    output = run_quote(source, {"symbol": "000001", "market": "CN"})
    assert output["status"] == "empty"
    assert "quote source unavailable" in output["degraded_reason"]


def test_get_quote_input_schema_rejects_extra_fields():
    from backend.tool_registry import ToolRegistryError, build_default_registry

    registry = build_default_registry()
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute(
            "get_quote", {"symbol": "000001", "evil": "injection"}
        ))
