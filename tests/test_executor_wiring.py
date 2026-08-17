"""Tests for downstream tool input wiring in the research executor.

Verifies the data-flow contract between chained tools:
- ``extract_financial_facts`` receives the texts of already-succeeded
  search/retrieval/read steps (injected as ``texts``);
- ``calculate_financial_metrics`` receives the extracted facts (injected as
  ``facts``);
- explicit values already present in the planner payload are never
  overwritten.
"""

import asyncio
import json

import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner
from backend.planner import DeterministicPlanner
from backend.policy import PolicyGate
from backend.research_executor import ResearchExecutor
from backend.schemas import (
    PlanStep,
    ResearchCreate,
    ResearchPlan,
    RouteDecision,
    SecurityCandidate,
)
from backend.tool_registry import (
    CalculateFinancialMetricsInput,
    ExtractFinancialFactsInput,
    FinancialFactsResult,
    FinancialMetricsResult,
    HybridRetrievalInput,
    HybridRetrievalResult,
    SearchFilingsInput,
    SearchToolResult,
    ToolRegistry,
    ToolSpec,
)

INPUT_MODELS = {
    "search_filings": SearchFilingsInput,
    "search_web": SearchFilingsInput,
    "retrieve_documents": HybridRetrievalInput,
    "extract_financial_facts": ExtractFinancialFactsInput,
    "calculate_financial_metrics": CalculateFinancialMetricsInput,
}
OUTPUT_MODELS = {
    "search_filings": SearchToolResult,
    "search_web": SearchToolResult,
    "retrieve_documents": HybridRetrievalResult,
    "extract_financial_facts": FinancialFactsResult,
    "calculate_financial_metrics": FinancialMetricsResult,
}


def route():
    return RouteDecision(
        intent="RESEARCH_NEW", confidence=1, requires_planner=True,
        external_research_allowed=False, response_policy="await_entity_resolution",
    )


def build_executor(tmp_path, *, handlers, plan=None):
    repository = Repository(tmp_path / "wiring.db")
    repository.initialize()
    runner = DurableRunner(repository)
    if plan is None:
        entity = SecurityCandidate(
            candidate_id="HK:0700.HK", company="腾讯控股", symbol="0700.HK",
            market="HK", confidence=1, matched_alias="腾讯",
        )
        plan = DeterministicPlanner().create_plan(
            question="分析腾讯盈利质量", entity=entity, depth="quick", budget_limit=50,
            version=1,
        )
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", symbol="0700.HK", market="HK", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="wiring-run",
        initial_plan=plan.model_dump(),
    )
    registry = ToolRegistry()
    for step in plan.steps:
        registry.register(ToolSpec(
            name=step.tool_name, version="1", risk_level="low", timeout_seconds=5,
            max_retries=1, idempotent=True, cost_class=1,
            requires_confirmation=False,
            input_model=INPUT_MODELS[step.tool_name],
            output_model=OUTPUT_MODELS[step.tool_name],
        ), handlers[step.tool_name])
    executor = ResearchExecutor(runner, registry, PolicyGate(repository, registry))
    return repository, runner, created, plan, executor


def run_batch(executor, created):
    return asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=50,
    ))


def test_extract_receives_upstream_search_texts(tmp_path):
    calls: dict[str, list] = {"extract": []}

    async def search_filings(payload):
        return {
            "status": "ok",
            "data": [
                {"source_id": "https://example.com/a", "title": "A", "url": "https://example.com/a",
                 "snippet": "2025 年营业收入 100.5 亿元。"},
                {"source_id": "https://example.com/b", "title": "B", "url": "https://example.com/b",
                 "snippet": "净利润 15.2 亿元。"},
            ],
            "evidence": [],
        }

    async def retrieve_documents(payload):
        return {"status": "empty", "data": [], "evidence": [],
                "retrieval": {"backend": "milvus", "mode": "hybrid", "fusion": "rrf",
                              "sparse": "bm25", "dense_model_version": None, "index_version": None}}

    async def extract_financial_facts(payload):
        calls["extract"].append(payload.model_dump())
        return {"status": "ok", "data": [], "evidence": []}

    async def calculate_financial_metrics(payload):
        return {"status": "empty", "data": []}

    repo, _runner, created, _plan, executor = build_executor(tmp_path, handlers={
        "search_filings": search_filings,
        "retrieve_documents": retrieve_documents,
        "extract_financial_facts": extract_financial_facts,
        "calculate_financial_metrics": calculate_financial_metrics,
    })
    first = run_batch(executor, created)
    assert set(first.executed_step_ids) == {"search_filings", "retrieve_documents"}
    run_batch(executor, created)  # extract_facts becomes ready

    assert len(calls["extract"]) == 1
    texts = calls["extract"][0].get("texts") or []
    assert len(texts) == 2
    assert texts[0]["source_id"] == "https://example.com/a"
    assert "100.5 亿元" in texts[0]["text"]
    assert texts[1]["source_id"] == "https://example.com/b"


def test_calculate_receives_extracted_facts(tmp_path):
    calls: dict[str, list] = {"extract": [], "calculate": []}
    extracted_facts = [
        {"name": "营收", "value": 100.5, "period": "2025", "unit": "亿元", "currency": "CNY",
         "source_id": "https://example.com/a"},
        {"name": "净利润", "value": 15.2, "period": "2025", "unit": "亿元", "currency": "CNY",
         "source_id": "https://example.com/b"},
    ]

    async def search_filings(payload):
        return {"status": "empty", "data": [], "evidence": []}

    async def retrieve_documents(payload):
        return {"status": "empty", "data": [], "evidence": [],
                "retrieval": {"backend": "milvus", "mode": "hybrid", "fusion": "rrf",
                              "sparse": "bm25", "dense_model_version": None, "index_version": None}}

    async def extract_financial_facts(payload):
        calls["extract"].append(payload.model_dump())
        return {"status": "ok", "data": extracted_facts, "evidence": []}

    async def calculate_financial_metrics(payload):
        calls["calculate"].append(payload.model_dump())
        return {"status": "ok", "data": [], "evidence": []}

    repo, _runner, created, _plan, executor = build_executor(tmp_path, handlers={
        "search_filings": search_filings,
        "retrieve_documents": retrieve_documents,
        "extract_financial_facts": extract_financial_facts,
        "calculate_financial_metrics": calculate_financial_metrics,
    })
    run_batch(executor, created)   # search + retrieve
    run_batch(executor, created)   # extract
    run_batch(executor, created)   # calculate

    assert len(calls["calculate"]) == 1
    facts = calls["calculate"][0].get("facts") or []
    assert facts == [
        {"name": "营收", "value": 100.5, "period": "2025", "unit": "亿元", "currency": "CNY"},
        {"name": "净利润", "value": 15.2, "period": "2025", "unit": "亿元", "currency": "CNY"},
    ]


def test_explicit_facts_are_not_overwritten(tmp_path):
    calls: dict[str, list] = {"calculate": []}

    async def calculate_financial_metrics(payload):
        calls["calculate"].append(payload.model_dump())
        return {"status": "ok", "data": [], "evidence": []}

    explicit = [{"name": "营收", "value": 999.0, "period": "2024", "unit": "亿元", "currency": "CNY"}]
    plan = ResearchPlan(
        version=1,
        goal="测试显式输入优先",
        steps=[
            PlanStep(
                id="calculate_metrics", tool_name="calculate_financial_metrics",
                dependencies=[], input={"metrics": ["roe"], "facts": explicit},
                success_criteria=["x"], estimated_cost=1,
            ),
        ],
    )
    repo, _runner, created, _plan, executor = build_executor(tmp_path, handlers={
        "calculate_financial_metrics": calculate_financial_metrics,
    }, plan=plan)
    run_batch(executor, created)

    facts = calls["calculate"][0].get("facts") or []
    assert facts == explicit


def test_no_upstream_outputs_injects_empty_lists(tmp_path):
    calls: dict[str, list] = {"extract": []}

    async def extract_financial_facts(payload):
        calls["extract"].append(payload.model_dump())
        return {"status": "empty", "data": [], "evidence": []}

    plan = ResearchPlan(
        version=1,
        goal="测试无上游数据注入",
        steps=[
            PlanStep(
                id="extract_facts", tool_name="extract_financial_facts",
                dependencies=[], input={"periods": 3},
                success_criteria=["x"], estimated_cost=1,
            ),
        ],
    )
    repo, _runner, created, _plan, executor = build_executor(tmp_path, handlers={
        "extract_financial_facts": extract_financial_facts,
    }, plan=plan)
    run_batch(executor, created)

    assert calls["extract"][0].get("texts") == []


def test_completed_outputs_filter_by_plan_version():
    """Downstream inputs only see the CURRENT plan version's succeeded steps."""
    snapshot = {
        "run": {"id": "run_1"},
        "steps": [
            {
                "id": "run_1:search_filings", "plan_version": 1, "status": "succeeded",
                "output_json": json.dumps({"status": "ok", "data": [
                    {"source_id": "v1-doc", "text": "旧计划的文本"},
                ]}),
            },
            {
                "id": "run_1:extract_facts", "plan_version": 2, "status": "succeeded",
                "output_json": json.dumps({"status": "ok", "data": [
                    {"name": "营收", "value": 1.0, "period": "2025", "unit": "亿元",
                     "currency": "CNY", "source_id": "v2-doc"},
                ]}),
            },
            {"id": "run_1:failed_step", "plan_version": 2, "status": "failed", "output_json": None},
            {"id": "run_1:malformed", "plan_version": 2, "status": "succeeded", "output_json": "not-json"},
        ],
    }
    outputs = ResearchExecutor._completed_outputs(snapshot, plan_version=2)
    assert set(outputs) == {"extract_facts"}
    assert outputs["extract_facts"]["data"][0]["name"] == "营收"
