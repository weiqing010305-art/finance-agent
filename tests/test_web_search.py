"""Tests for the controlled web search and filings tools."""

import asyncio
import json

import httpx
import pytest

from backend.tool_registry import (
    SearchFilingsInput,
    SearchWebInput,
    ToolRegistryError,
    build_default_registry,
)
from backend.web_search import (
    DeepSeekWebSearch,
    _is_official_filing_domain,
    search_filings,
    search_web,
)


def citation(url, title, content=""):
    return {
        "type": "url_citation",
        "url_citation": {"url": url, "title": title, "content": content},
    }


def response_with(annotations):
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "ok", "annotations": annotations}
                ],
            }
        ]
    }


def make_client(annotations, *, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(json.loads(request.content))
        return httpx.Response(200, json=response_with(annotations))

    return DeepSeekWebSearch(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(handler),
    )


def run_search_web(client, payload):
    return asyncio.run(search_web(
        SearchWebInput.model_validate(payload), _client=client
    ))


def run_filings(client, payload):
    return asyncio.run(search_filings(
        SearchFilingsInput.model_validate(payload), _client=client
    ))


PUBLIC_HIT = citation("https://finance.example.com/report", "示例研报", "摘要内容")


def test_search_web_returns_normalized_hits():
    hits = [
        citation("https://example.com/a", "A 标题", "A 摘要"),
        citation("https://example.com/b", "B 标题", "B 摘要"),
    ]
    output = run_search_web(make_client(hits), {"query": "腾讯 财报", "max_results": 8})
    assert output["status"] == "ok"
    assert not output["degraded"]
    assert [hit["title"] for hit in output["data"]] == ["A 标题", "B 标题"]
    assert output["evidence"][0]["url"] == "https://example.com/a"
    assert output["evidence"][0]["publisher"] == "example.com"


def test_search_web_empty_results_degrades():
    output = run_search_web(make_client([]), {"query": "无结果查询"})
    assert output["status"] == "empty"
    assert output["data"] == []
    assert "no search results" in output["degraded_reason"]


def test_search_web_without_api_key_degrades(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    output = asyncio.run(search_web(SearchWebInput.model_validate({"query": "x"})))
    assert output["status"] == "empty"
    assert "DEEPSEEK_API_KEY" in output["degraded_reason"]


def test_search_web_filters_unsafe_urls():
    hits = [
        citation("https://example.com/ok", "安全来源"),
        citation("http://127.0.0.1/secret", "内网来源"),
        citation("http://192.168.1.1/admin", "私网来源"),
        citation("file:///etc/passwd", "非 http"),
    ]
    output = run_search_web(make_client(hits), {"query": "x"})
    assert [hit["url"] for hit in output["data"]] == ["https://example.com/ok"]


def test_search_web_respects_max_results():
    hits = [citation(f"https://example.com/{i}", f"来源 {i}") for i in range(20)]
    output = run_search_web(make_client(hits), {"query": "x", "max_results": 3})
    assert len(output["data"]) == 3


def test_search_web_sends_query_in_payload():
    captured = []
    client = make_client([PUBLIC_HIT], capture=captured)
    run_search_web(client, {"query": "比亚迪 年报", "max_results": 5})
    body = captured[0]
    assert body["tools"] == [{"type": "web_search"}]
    assert body["input"] == "比亚迪 年报"
    assert body["model"]


def test_official_domain_check():
    assert _is_official_filing_domain("www.cninfo.com.cn")
    assert _is_official_filing_domain("static.sse.com.cn")
    assert _is_official_filing_domain("www1.hkexnews.hk")
    assert _is_official_filing_domain("www.sec.gov")
    assert not _is_official_filing_domain("www.example-blog.com")
    assert not _is_official_filing_domain("cninfo.com.cn.evil.com")


def test_search_filings_keeps_only_official_domains():
    hits = [
        citation("https://static.cninfo.com.cn/finalpage/2025-01-01/xx.pdf", "巨潮公告"),
        citation("https://www1.hkexnews.hk/listedco/listconews/2025/xx.pdf", "港交所披露"),
        citation("https://blog.example.com/tencent-analysis", "非官方博客"),
    ]
    output = run_filings(make_client(hits), {"company": "腾讯控股"})
    assert output["status"] == "ok"
    assert len(output["data"]) == 2
    urls = [hit["url"] for hit in output["data"]]
    assert "https://blog.example.com/tencent-analysis" not in urls


def test_search_filings_without_official_results_degrades():
    hits = [citation("https://blog.example.com/tencent-analysis", "非官方博客")]
    output = run_filings(make_client(hits), {"company": "腾讯控股"})
    assert output["status"] == "empty"
    assert "official" in output["degraded_reason"]


def test_search_filings_builds_query_from_company_and_types():
    captured = []
    client = make_client([citation("https://www.cninfo.com.cn/x", "公告")], capture=captured)
    run_filings(client, {"company": "比亚迪", "document_types": ["annual_report"]})
    query = captured[0]["input"]
    assert "比亚迪" in query
    assert "annual_report" in query


def test_registry_wiring_uses_injected_search_handlers():
    fake = make_client([PUBLIC_HIT])
    registry = build_default_registry(search_handlers={
        "search_web": lambda payload, context=None: search_web(payload, context, _client=fake),
        "search_filings": lambda payload, context=None: search_filings(payload, context, _client=fake),
    })
    execution = asyncio.run(registry.execute(
        "search_web", {"query": "腾讯 财报", "max_results": 5}
    ))
    assert execution.output["status"] == "ok"
    assert len(execution.output["data"]) == 1
    assert "unconfigured" not in str(execution.output)


def test_registry_without_api_key_degrades_not_crash(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    registry = build_default_registry()
    execution = asyncio.run(registry.execute(
        "search_web", {"query": "腾讯 财报", "max_results": 5}
    ))
    assert execution.output["status"] == "empty"
    assert execution.output["degraded"] is True
    assert "DEEPSEEK_API_KEY" in execution.output["degraded_reason"]


def test_search_input_schema_rejects_extra_fields():
    registry = build_default_registry()
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute(
            "search_web", {"query": "x", "evil": "injection"}
        ))
