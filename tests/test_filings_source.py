"""Tests for the real cninfo filings source and the search_filings fallback chain."""

import asyncio
import json

import httpx
import pytest

from backend.filings_source import CninfoFilingsSource, FilingSourceError
from backend.tool_registry import SearchFilingsInput
from backend.web_search import search_filings

ANNOUNCEMENTS = [
    {
        "announcementTitle": "平安银行股份有限公司2026年半年度报告摘要",
        "secName": "平安银行", "secCode": "000001",
        "announcementTime": 1786723200000,
        "adjunctUrl": "finalpage/2026-08-15/1225475808.PDF",
    },
    {
        "announcementTitle": "董事会决议公告",
        "secName": "平安银行", "secCode": "000001",
        "announcementTime": 1786723200000,
        "adjunctUrl": "finalpage/2026-08-15/1225475348.PDF",
    },
]


def make_source(payload_json, *, status=200, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(dict(request.url.params))
            capture.append(request.read().decode())
        if status >= 400:
            return httpx.Response(status, text="boom")
        return httpx.Response(status, json=payload_json)

    return CninfoFilingsSource(transport=httpx.MockTransport(handler))


def run_filings(source, web_client, payload):
    return asyncio.run(search_filings(
        SearchFilingsInput.model_validate(payload),
        _client=web_client, _filings_source=source,
    ))


class FakeWebSearch:
    def __init__(self, hits):
        self.hits = hits

    async def search(self, query, *, max_results=8):
        return self.hits


def web_hit(url):
    from backend.web_search import SearchHitData
    return SearchHitData(url=url, title="标题", snippet="摘要")


def test_cninfo_parses_announcements():
    source = make_source({"totalAnnouncement": 2, "announcements": ANNOUNCEMENTS})
    hits = asyncio.run(source.search(company="平安银行", max_results=10))
    assert len(hits) == 2
    first = hits[0]
    assert first.url == "http://static.cninfo.com.cn/finalpage/2026-08-15/1225475808.PDF"
    assert "半年度报告摘要" in first.title
    assert first.publisher == "巨潮资讯"
    assert "000001" in first.snippet
    assert "2026" in first.snippet  # publication date from the timestamp


def test_cninfo_sends_searchkey_and_size():
    captured = []
    source = make_source({"totalAnnouncement": 0, "announcements": []}, capture=captured)
    asyncio.run(source.search(company="平安银行", document_types=["annual_report"], max_results=5))
    body = captured[1]
    assert "searchkey=%E5%B9%B3%E5%AE%89%E9%93%B6%E8%A1%8C" in body  # 平安银行
    assert "pageSize=5" in body
    assert "category_ndbg_szsh" in body  # annual report mapped to cninfo category


def test_cninfo_empty_results_return_empty_list():
    source = make_source({"totalAnnouncement": 0, "announcements": []})
    assert asyncio.run(source.search(company="不存在公司", max_results=10)) == []


def test_cninfo_missing_announcements_raises():
    source = make_source({"totalAnnouncement": 0})
    with pytest.raises(FilingSourceError):
        asyncio.run(source.search(company="平安银行"))


def test_cninfo_http_error_raises():
    source = make_source({}, status=500)
    with pytest.raises(FilingSourceError, match="HTTP 500"):
        asyncio.run(source.search(company="平安银行"))


def test_cninfo_skips_entries_without_pdf_url():
    broken = dict(ANNOUNCEMENTS[0], adjunctUrl="")
    source = make_source({"announcements": [broken, ANNOUNCEMENTS[1]]})
    hits = asyncio.run(source.search(company="平安银行"))
    assert len(hits) == 1


def test_search_filings_uses_cninfo_primary_source():
    source = make_source({"totalAnnouncement": 1, "announcements": ANNOUNCEMENTS[:1]})
    output = run_filings(source, None, {"company": "平安银行", "market": "CN"})
    assert output["status"] == "ok"
    assert not output["degraded"]
    assert output["fallback_used"] is None
    assert output["data"][0]["url"].startswith("http://static.cninfo.com.cn/")
    assert output["evidence"][0]["publisher"] == "巨潮资讯"


def test_search_filings_degrades_to_web_on_source_failure():
    source = make_source({}, status=503)
    web = FakeWebSearch([web_hit("https://www.cninfo.com.cn/x.pdf")])
    output = run_filings(source, web, {"company": "平安银行", "market": "CN"})
    assert output["status"] == "ok"
    assert output["degraded"] is True
    assert output["fallback_used"] == "web_search"
    assert "cninfo" in output["degraded_reason"]


def test_search_filings_degrades_to_web_on_empty_source():
    source = make_source({"totalAnnouncement": 0, "announcements": []})
    web = FakeWebSearch([web_hit("https://www.sse.com.cn/x.pdf")])
    output = run_filings(source, web, {"company": "平安银行", "market": "CN"})
    assert output["fallback_used"] == "web_search"
    assert "no announcements" in output["degraded_reason"]


def test_search_filings_hk_market_skips_cninfo():
    # HK is not covered by cninfo: fall through to web search immediately.
    source = make_source({"totalAnnouncement": 1, "announcements": ANNOUNCEMENTS[:1]})
    web = FakeWebSearch([web_hit("https://www1.hkexnews.hk/x.pdf")])
    output = run_filings(source, web, {"company": "腾讯控股", "market": "HK"})
    assert output["fallback_used"] == "web_search"
    assert "HK" in output["degraded_reason"]
    assert output["data"][0]["url"].startswith("https://www1.hkexnews.hk/")


def test_search_filings_no_fallback_without_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    source = make_source({}, status=500)
    output = run_filings(source, None, {"company": "平安银行", "market": "CN"})
    assert output["status"] == "empty"
    assert "cninfo" in output["degraded_reason"]
    assert "DEEPSEEK_API_KEY" in output["degraded_reason"]
