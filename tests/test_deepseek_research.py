import asyncio
import json

import httpx
import pytest

from backend.database import Repository
from backend.deepseek_research import DeepSeekConfig, DeepSeekResearchClient
from backend.research import run_deepseek_research
from backend.schemas import ResearchCreate


def test_deepseek_responses_api_streams_web_search_and_normalizes_citations():
    captured = {}
    observed_events = []
    report = {
        "company": "中国银行股份有限公司",
        "symbol": "601988.SH",
        "market": "CN",
        "title": "中国银行研究",
        "summary": "盈利保持稳定，但息差仍需跟踪。",
        "sources": [
            {
                "url": "https://www.boc.cn/report.pdf",
                "title": "中国银行2025年度报告",
                "publisher": "中国银行",
                "excerpt": "本报告披露集团年度经营成果与财务状况。",
            }
        ],
        "sections": [
            {
                "key": "conclusion",
                "title": "核心结论",
                "content": "公司披露了最新经营数据。",
                "points": [
                    {"label": "盈利表现", "text": "净利润保持稳定。"},
                    {"label": "待跟踪项", "text": "仍需观察净息差。"},
                ],
                "source_urls": ["https://www.boc.cn/report.pdf"],
            }
        ],
    }
    completed = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(report, ensure_ascii=False),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://www.boc.cn/report.pdf",
                                    "title": "中国银行年度报告",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    }
    stream = "\n\n".join(
        [
            'data: {"type":"response.web_search_call.searching","item_id":"ws-1"}',
            'data: {"type":"response.output_item.done","item":{"type":"web_search_call","action":{"type":"search","query":"中国银行 年报"}}}',
            f"data: {json.dumps(completed, ensure_ascii=False)}",
        ]
    ) + "\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=stream.encode("utf-8"), headers={"content-type": "text/event-stream"})

    client = DeepSeekResearchClient(
        DeepSeekConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )
    normalized, evidence = asyncio.run(
        client.research(
            {
                "company": "自动识别中",
                "symbol": None,
                "market": "AUTO",
                "question": "中国银行怎么样？",
            },
            on_event=observed_events.append,
        )
    )

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["tools"] == [{"type": "web_search"}]
    assert captured["stream"] is True
    assert "max_output_tokens" not in captured
    assert [event["type"] for event in observed_events] == [
        "response.web_search_call.searching",
        "response.output_item.done",
        "response.completed",
    ]
    assert normalized["company"] == "中国银行股份有限公司"
    assert normalized["sections"][0]["citations"] == [1]
    assert normalized["sections"][0]["points"] == [
        {"label": "盈利表现", "text": "净利润保持稳定。"},
        {"label": "待跟踪项", "text": "仍需观察净息差。"},
    ]
    assert evidence[0]["title"] == "中国银行2025年度报告"
    assert evidence[0]["publisher"] == "中国银行"
    assert evidence[0]["excerpt"] == "本报告披露集团年度经营成果与财务状况。"
    assert evidence[0]["url"] == "https://www.boc.cn/report.pdf"


def test_deepseek_incomplete_stream_reports_provider_reason():
    stream = "\n\n".join([
        'data: {"type":"response.web_search_call.completed","item_id":"ws-1"}',
        'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}',
    ]) + "\n\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream.encode("utf-8"), headers={"content-type": "text/event-stream"})

    client = DeepSeekResearchClient(
        DeepSeekConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(Exception, match="max_output_tokens"):
        asyncio.run(client.research({"company": "浦发银行", "question": "浦发银行怎么样"}))


def test_deepseek_recovers_when_search_completes_without_report_text():
    requests = []
    observed_events = []
    recovered_report = {
        "company": "广东华润银行股份有限公司",
        "symbol": "",
        "market": "CN",
        "title": "广东华润银行研究",
        "summary": "已根据上一轮检索结果重新整理报告。",
        "sources": [],
        "sections": [{
            "key": "conclusion",
            "title": "核心结论",
            "content": "报告恢复成功。",
            "points": [],
            "source_urls": [],
        }],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            completed_without_text = {
                "type": "response.completed",
                "response": {
                    "id": "resp-search-only",
                    "output": [{"type": "web_search_call", "id": "ws-1"}],
                },
            }
            stream = f"data: {json.dumps(completed_without_text)}\n\n"
            return httpx.Response(200, content=stream.encode(), headers={"content-type": "text/event-stream"})
        recovered = {
            "id": "resp-recovered",
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps(recovered_report, ensure_ascii=False),
                    "annotations": [],
                }],
            }],
        }
        return httpx.Response(200, json=recovered)

    client = DeepSeekResearchClient(
        DeepSeekConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )
    report, _evidence = asyncio.run(
        client.research(
            {"company": "自动识别中", "market": "AUTO", "question": "华润银行怎么样？"},
            on_event=observed_events.append,
        )
    )

    assert report["title"] == "广东华润银行研究"
    assert len(requests) == 2
    assert requests[1]["previous_response_id"] == "resp-search-only"
    assert requests[1]["stream"] is False
    assert "tools" not in requests[1]
    assert any(event["type"] == "finscope.report_recovery" for event in observed_events)


def test_deepseek_failure_keeps_browsed_pages_as_partial_evidence(tmp_path):
    repository = Repository(tmp_path / "partial-evidence.db")
    repository.initialize()
    task = repository.create_task(
        ResearchCreate(company="自动识别中", market="AUTO", question="华润银行怎么样？")
    )

    class SearchThenFailClient:
        async def research(self, _task, *, on_event=None):
            on_event({
                "type": "response.output_item.done",
                "item": {
                    "type": "web_search_call",
                    "action": {
                        "type": "open_page",
                        "url": "https://www.crbank.com.cn/about/#ws_call_id=call_1",
                    },
                },
            })
            raise RuntimeError("DeepSeek 响应没有报告文本")

    asyncio.run(run_deepseek_research(repository, task["id"], SearchThenFailClient()))

    failed = repository.get_task(task["id"])
    assert failed["status"] == "failed"
    assert len(failed["evidence"]) == 1
    assert failed["evidence"][0]["url"] == "https://www.crbank.com.cn/about/"
    assert failed["evidence"][0]["title"] == "crbank.com.cn 网页内容"


def test_deepseek_execution_streams_readable_report_draft(tmp_path):
    repository = Repository(tmp_path / "deepseek-draft.db")
    repository.initialize()
    task = repository.create_task(
        ResearchCreate(company="自动识别中", market="AUTO", question="中国银行怎么样？")
    )
    report = {
        "company": "中国银行股份有限公司",
        "symbol": "601988.SH",
        "market": "CN",
        "title": "中国银行研究",
        "summary": "盈利保持稳定。",
        "sources": [],
        "sections": [{
            "key": "conclusion",
            "title": "核心结论",
            "content": "报告已生成。",
            "points": [],
            "source_urls": [],
        }],
    }

    class DraftClient:
        async def research(self, _task, *, on_event=None):
            on_event({"type": "response.output_text.delta", "delta": "REPORT_DRAFT:\n### 核心结论\n"})
            on_event({"type": "response.output_text.delta", "delta": "- 盈利保持稳定。\nFINAL_"})
            on_event({"type": "response.output_text.delta", "delta": "JSON:" + json.dumps(report, ensure_ascii=False)})
            return report, []

    asyncio.run(run_deepseek_research(repository, task["id"], DraftClient()))

    events = repository.list_events(task["id"])
    draft_events = [event for event in events if event["kind"] == "task.report_delta"]
    assert [event["payload"]["delta"] for event in draft_events] == [
        "\n### 核心结论\n",
        "- 盈利保持稳定。\n",
    ]
    assert not any("FINAL_JSON" in event["payload"]["delta"] for event in draft_events)


def test_deepseek_execution_persists_real_search_and_browse_actions(tmp_path):
    repository = Repository(tmp_path / "deepseek-trace.db")
    repository.initialize()
    task = repository.create_task(
        ResearchCreate(company="自动识别中", market="AUTO", question="中国银行怎么样？")
    )

    class FakeClient:
        async def research(self, _task, *, on_event=None):
            on_event({"type": "response.web_search_call.searching"})
            on_event({
                "type": "response.output_item.done",
                "item": {
                    "type": "web_search_call",
                    "action": {"type": "search", "query": "中国银行 2025 年报"},
                },
            })
            on_event({
                "type": "response.output_item.done",
                "item": {
                    "type": "web_search_call",
                    "action": {"type": "open_page", "url": "https://www.boc.cn/report.pdf"},
                },
            })
            on_event({"type": "response.output_text.delta", "delta": "{"})
            return (
                {
                    "company": "中国银行股份有限公司",
                    "symbol": "601988.SH",
                    "market": "CN",
                    "title": "中国银行研究",
                    "summary": "摘要",
                    "sections": [],
                    "synthetic": False,
                    "provider": "deepseek",
                },
                [{
                    "citation_number": 1,
                    "title": "中国银行年度报告",
                    "publisher": "boc.cn",
                    "url": "https://www.boc.cn/report.pdf",
                    "source_type": "网页来源",
                    "excerpt": "年度报告",
                    "agent": "财报分析 Agent",
                }],
            )

    asyncio.run(run_deepseek_research(repository, task["id"], FakeClient()))

    events = repository.list_events(task["id"])
    messages = [event["message"] for event in events]
    assert "搜索：中国银行 2025 年报" in messages
    assert "浏览页面：https://www.boc.cn/report.pdf" in messages
    search_event = next(event for event in events if event["message"].startswith("搜索："))
    assert search_event["payload"]["query"] == "中国银行 2025 年报"
    browse_event = next(event for event in events if event["message"].startswith("浏览页面："))
    assert browse_event["payload"]["url"] == "https://www.boc.cn/report.pdf"
    completed = repository.get_task(task["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["provider"] == "deepseek"


def test_deepseek_execution_preserves_exception_type_when_message_is_empty(tmp_path):
    repository = Repository(tmp_path / "empty-error.db")
    repository.initialize()
    task = repository.create_task(
        ResearchCreate(company="自动识别中", market="AUTO", question="招商证券怎么样")
    )

    class TimeoutClient:
        async def research(self, _task, *, on_event=None):
            raise httpx.ReadTimeout("")

    asyncio.run(run_deepseek_research(repository, task["id"], TimeoutClient()))

    failed = repository.get_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "ReadTimeout"
    assert "ReadTimeout" in repository.list_events(task["id"])[-1]["message"]


def test_url_only_evidence_is_enriched_from_page_metadata():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            text="""<!doctype html><html><head>
            <meta property="og:site_name" content="浦发银行">
            <meta property="og:title" content="浦发银行发布2025年度业绩报告">
            <meta name="description" content="浦发银行披露年度经营情况与主要财务指标。">
            <title>备用标题</title></head><body></body></html>""",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    client = DeepSeekResearchClient(
        DeepSeekConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )
    evidence = [{
        "citation_number": 1,
        "title": "https://news.spdb.com.cn/report.shtml",
        "publisher": "news.spdb.com.cn",
        "url": "https://news.spdb.com.cn/report.shtml",
        "source_type": "网页来源",
        "excerpt": "点击访问原始来源并核对完整上下文。",
        "agent": "财报分析 Agent",
    }]

    enriched = asyncio.run(client.enrich_evidence(evidence))

    assert enriched[0]["title"] == "浦发银行发布2025年度业绩报告"
    assert enriched[0]["publisher"] == "浦发银行"
    assert enriched[0]["excerpt"] == "浦发银行披露年度经营情况与主要财务指标。"
