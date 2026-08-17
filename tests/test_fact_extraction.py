"""Tests for the deterministic financial fact extractor."""

import asyncio

import pytest

from backend.fact_extraction import (
    FactExtractor,
    extract_financial_facts,
    split_sentences,
)
from backend.tool_registry import (
    ExtractFinancialFactsInput,
    ToolRegistryError,
    build_default_registry,
)


def text(source_id: str, content: str) -> dict:
    return {"source_id": source_id, "text": content}


def run(payload: dict) -> dict:
    return asyncio.run(extract_financial_facts(
        ExtractFinancialFactsInput.model_validate(payload)
    ))


def facts_by_name(output: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in output["data"]:
        grouped.setdefault(item["name"], []).append(item)
    return grouped


def test_extracts_single_period_facts():
    output = run({
        "texts": [text(
            "doc-1",
            "2025 年营业收入 100.5 亿元，净利润 15.2 亿元，总资产 500 亿元。",
        )],
    })
    assert output["status"] == "ok"
    by_name = facts_by_name(output)
    assert by_name["营收"][0]["value"] == 100.5
    assert by_name["营收"][0]["period"] == "2025"
    assert by_name["营收"][0]["unit"] == "亿元"
    assert by_name["营收"][0]["currency"] == "CNY"
    assert by_name["营收"][0]["source_id"] == "doc-1"
    assert by_name["净利润"][0]["value"] == 15.2
    assert by_name["总资产"][0]["value"] == 500.0


def test_extracts_multiple_periods_and_keeps_recent_ones():
    output = run({
        "texts": [text(
            "doc-1",
            "2023 年营业收入 70 亿元。2024 年营业收入 80 亿元。2025 年营业收入 100 亿元。",
        )],
        "periods": 2,
    })
    revenues = facts_by_name(output)["营收"]
    periods = sorted(item["period"] for item in revenues)
    assert periods == ["2024", "2025"]  # only the two most recent periods


def test_extracts_fy_prefixed_periods():
    output = run({
        "texts": [text("doc-1", "FY2025 净利润 15.2 亿元，同比增长 8%。")],
    })
    assert facts_by_name(output)["净利润"][0]["period"] == "FY2025"


def test_ignores_bare_numbers_without_units():
    output = run({
        "texts": [text("doc-1", "2025 年营收 100.5 亿元；员工人数增长 12%。")],
    })
    by_name = facts_by_name(output)
    assert "营收" in by_name
    # "增长 12%" carries no amount unit -> not a fact; no bogus facts from it
    assert len(output["data"]) == 1


def test_longest_alias_wins_for_parent_profit():
    output = run({
        "texts": [text(
            "doc-1",
            "2025 年归属于母公司股东的净利润 14.9 亿元；净利润 15.2 亿元。",
        )],
    })
    assert facts_by_name(output)["净利润"][0]["value"] == 14.9  # parent first


def test_currency_detection():
    output = run({
        "texts": [text("doc-1", "2025 年营业收入 100 亿美元，净利润 10 亿港元。")],
    })
    by_name = facts_by_name(output)
    assert by_name["营收"][0]["currency"] == "USD"
    assert by_name["净利润"][0]["currency"] == "HKD"
    assert by_name["营收"][0]["unit"] == "亿美元"


def test_missing_period_skips_fact():
    output = run({
        "texts": [text("doc-1", "营业收入 100 亿元（无期间标记）。")],
    })
    assert output["status"] == "empty"
    assert "no financial facts" in output["degraded_reason"]


def test_no_texts_degrades_honestly():
    output = run({"texts": []})
    assert output["status"] == "empty"
    assert "read_document" in output["degraded_reason"]


def test_deduplicates_identical_facts():
    output = run({
        "texts": [
            text("doc-1", "2025 年营业收入 100 亿元。"),
            text("doc-2", "2025 年营业收入 100 亿元。"),
        ],
    })
    revenues = facts_by_name(output).get("营收", [])
    assert len(revenues) == 2  # same fact from two sources is kept (traceable)


def test_negative_values_extracted():
    output = run({
        "texts": [text("doc-1", "2025 年净利润 -3.5 亿元，同比转亏。")],
    })
    assert facts_by_name(output)["净利润"][0]["value"] == -3.5


def test_registry_wiring_returns_real_facts():
    registry = build_default_registry()
    execution = asyncio.run(registry.execute(
        "extract_financial_facts",
        {"texts": [text("doc-1", "2025 年营业收入 100.5 亿元，净利润 15 亿元。")], "periods": 3},
    ))
    assert execution.output["status"] == "ok"
    assert len(execution.output["data"]) == 2
    assert "unconfigured" not in str(execution.output)


def test_extract_input_schema_rejects_extra_fields():
    registry = build_default_registry()
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute(
            "extract_financial_facts", {"texts": [], "evil": "injection"}
        ))


def test_split_sentences_handles_punctuation():
    sentences = split_sentences("2025 年营收 100 亿元。净利润 15 亿元；总资产 500 亿元！")
    assert len(sentences) == 3


def test_extractor_is_deterministic():
    payload = {
        "texts": [text("doc-1", "2025 年营业收入 100 亿元，净利润 15 亿元。")],
    }
    first = run(payload)
    second = run(payload)
    assert first == second
