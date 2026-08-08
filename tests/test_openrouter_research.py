import asyncio
import json

import httpx

from backend.database import Repository
from backend.openrouter_research import OpenRouterConfig, OpenRouterResearchClient
from backend.research import run_openrouter_research
from backend.schemas import ResearchCreate


def test_openrouter_payload_and_citation_normalization():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        report = {
            "company": "腾讯控股",
            "symbol": "0700.HK",
            "market": "HK",
            "title": "利润恢复但现金流仍需跟踪",
            "summary": "收入与利润改善，但投资节奏带来不确定性。",
            "sections": [
                {
                    "key": "financial-performance",
                    "title": "财务表现",
                    "content": "利润增速高于收入。",
                    "source_urls": ["https://www.hkexnews.hk/report.pdf"],
                },
                {
                    "key": "risks",
                    "title": "风险与未知",
                    "content": "资本开支需要继续跟踪。",
                    "source_urls": ["https://example.com/analysis"],
                },
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(report, ensure_ascii=False),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://www.hkexnews.hk/report.pdf",
                                        "title": "年度业绩公告",
                                        "content": "公司披露利润与收入变化。",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    client = OpenRouterResearchClient(
        OpenRouterConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )
    report, evidence = asyncio.run(
        client.research(
            {
                "company": "腾讯控股",
                "market": "HK",
                "symbol": "0700.HK",
                "depth": "standard",
                "question": "利润增长是否可持续？",
            }
        )
    )

    assert captured["model"] == "deepseek/deepseek-v4-flash-0731"
    assert captured["plugins"] == [
        {"id": "web", "engine": "parallel", "max_results": 6}
    ]
    assert captured["max_tokens"] == 1_400
    assert report["synthetic"] is False
    assert report["provider"] == "openrouter"
    assert report["company"] == "腾讯控股"
    assert report["symbol"] == "0700.HK"
    assert report["market"] == "HK"
    assert report["sections"][0]["citations"] == [1]
    assert report["sections"][1]["citations"] == [2]
    assert evidence[0]["source_type"] == "一手来源"
    assert evidence[1]["url"] == "https://example.com/analysis"


def test_code_fenced_json_is_accepted():
    parsed = OpenRouterResearchClient._parse_report(
        "```json\n{\"title\":\"T\",\"summary\":\"S\",\"sections\":[]}\n```"
    )
    assert parsed["title"] == "T"


def test_full_synthesis_reuses_evidence_without_web_search():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        report = {
            "company": "腾讯控股",
            "symbol": "0700.HK",
            "market": "HK",
            "title": "腾讯完整研究",
            "summary": "完整结论。",
            "sections": [
                {
                    "key": "conclusion",
                    "title": "核心结论",
                    "content": "利润增长得到公告支持。",
                    "source_urls": ["https://www.hkexnews.hk/report.pdf"],
                }
            ],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(report, ensure_ascii=False)}}]},
        )

    client = OpenRouterResearchClient(
        OpenRouterConfig(api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )
    report = asyncio.run(
        client.synthesize(
            {"company": "腾讯控股", "symbol": "0700.HK", "market": "HK", "question": "利润是否可持续？"},
            {"company": "腾讯控股", "summary": "快速结论", "sections": []},
            [
                {
                    "citation_number": 1,
                    "title": "年度业绩公告",
                    "url": "https://www.hkexnews.hk/report.pdf",
                    "excerpt": "公司披露利润增长。",
                }
            ],
        )
    )

    assert "plugins" not in captured
    assert captured["max_tokens"] == 2_600
    assert report["sections"][0]["citations"] == [1]


def test_openrouter_execution_exposes_auditable_work_trace(tmp_path):
    repository = Repository(tmp_path / "trace.db")
    repository.initialize()
    task = repository.create_task(
        ResearchCreate(company="自动识别中", market="AUTO", question="分析巨力索具的经营表现和风险")
    )

    class FakeClient:
        async def research(self, _task):
            return (
                {
                    "company": "巨力索具",
                    "symbol": "002342.SZ",
                    "market": "CN",
                    "title": "研究报告",
                    "summary": "摘要",
                    "sections": [],
                    "synthetic": False,
                },
                [
                    {
                        "citation_number": 1,
                        "title": "公司公告",
                        "publisher": "交易所",
                        "url": "https://example.com/notice",
                        "source_type": "一手来源",
                        "excerpt": "公告摘录",
                        "agent": "财报分析 Agent",
                    }
                ],
            )

        async def synthesize(self, _task, quick_report, _evidence):
            return quick_report

    asyncio.run(run_openrouter_research(repository, task["id"], FakeClient()))
    events = repository.list_events(task["id"])
    messages = [event["message"] for event in events]
    steps = [event["step"] for event in events]

    assert any("优先检索" in message for message in messages)
    assert any("1 条来源" in message for message in messages)
    assert "reading" in steps
    assert "writing" in steps
    resolved = repository.get_task(task["id"])
    assert resolved["company"] == "巨力索具"
    assert resolved["symbol"] == "002342.SZ"
    assert resolved["market"] == "CN"


def test_quick_result_is_persisted_before_full_synthesis(tmp_path):
    repository = Repository(tmp_path / "quick-first.db")
    repository.initialize()
    task = repository.create_task(
        ResearchCreate(company="自动识别中", market="AUTO", question="分析腾讯近期经营表现")
    )
    observed = {}

    quick_report = {
        "company": "腾讯控股",
        "symbol": "0700.HK",
        "market": "HK",
        "title": "腾讯快速研究",
        "summary": "这是第一阶段可见结论。",
        "sections": [],
        "synthetic": False,
        "provider": "openrouter",
    }
    final_report = {
        **quick_report,
        "title": "腾讯完整研究",
        "summary": "这是第二阶段完整结论。",
    }
    evidence = [
        {
            "citation_number": 1,
            "title": "腾讯公告",
            "publisher": "hkexnews.hk",
            "url": "https://www.hkexnews.hk/tencent.pdf",
            "source_type": "一手来源",
            "excerpt": "公告摘录",
            "agent": "财报分析 Agent",
        }
    ]

    class TwoStageClient:
        async def research(self, _task):
            return quick_report, evidence

        async def synthesize(self, _task, _quick_report, _evidence):
            interim = repository.get_task(task["id"])
            observed.update(
                status=interim["status"],
                step=interim["current_step"],
                summary=interim["result"]["summary"],
                evidence_count=len(interim["evidence"]),
            )
            return final_report

    asyncio.run(run_openrouter_research(repository, task["id"], TwoStageClient()))

    assert observed == {
        "status": "running",
        "step": "writing",
        "summary": "这是第一阶段可见结论。",
        "evidence_count": 1,
    }
    quick_event = next(
        event for event in repository.list_events(task["id"])
        if event["payload"] and event["payload"].get("phase") == "quick"
    )
    assert quick_event["step"] == "reading"
    completed = repository.get_task(task["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["summary"] == "这是第二阶段完整结论。"
