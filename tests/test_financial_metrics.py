"""Tests for the deterministic financial metrics calculator.

Covers the tool contract (schema validation, registry wiring) and the
arithmetic itself (correctness, aliases, period pairing, missing/zero inputs).
"""

import asyncio

import pytest

from backend.financial_metrics import (
    METRICS,
    build_index,
    calculate_financial_metrics,
    resolve_metric,
)
from backend.tool_registry import (
    CalculateFinancialMetricsInput,
    ToolRegistryError,
    build_default_registry,
)


def fact(name, value, period="FY2025", unit="", currency="CNY"):
    return {
        "name": name,
        "value": value,
        "period": period,
        "unit": unit,
        "currency": currency,
    }


BASE_FACTS = [
    fact("营收", 10000, "FY2025"),
    fact("营收", 8000, "FY2024"),
    fact("营业成本", 6000, "FY2025"),
    fact("净利润", 1500, "FY2025"),
    fact("股东权益", 5000, "FY2025"),
    fact("总资产", 20000, "FY2025"),
    fact("总负债", 12000, "FY2025"),
    fact("流动资产", 9000, "FY2025"),
    fact("流动负债", 4500, "FY2025"),
    fact("股本", 1000, "FY2025"),
]


def run(payload: dict) -> dict:
    return asyncio.run(calculate_financial_metrics(
        CalculateFinancialMetricsInput.model_validate(payload)
    ))


def values_by_name(output: dict) -> dict[str, float]:
    return {item["name"]: item["value"] for item in output["data"]}


# ---------------------------------------------------------------------------
# Arithmetic correctness (hand-computed expectations)
# ---------------------------------------------------------------------------


def test_core_metrics_computed_correctly():
    output = run({
        "facts": BASE_FACTS,
        "metrics": [
            "growth", "margin", "net_margin", "roe", "roa",
            "debt_ratio", "current_ratio", "asset_turnover", "eps",
        ],
    })
    assert output["status"] == "ok"
    assert not output["degraded"]
    values = values_by_name(output)
    assert values["营收增速"] == 25.0          # (10000-8000)/8000
    assert values["毛利率"] == 40.0            # (10000-6000)/10000
    assert values["净利率"] == 15.0            # 1500/10000
    assert values["净资产收益率"] == 30.0      # 1500/5000
    assert values["总资产收益率"] == 7.5       # 1500/20000
    assert values["资产负债率"] == 60.0        # 12000/20000
    assert values["流动比率"] == 2.0           # 9000/4500
    assert values["总资产周转率"] == 0.5       # 10000/20000
    assert values["每股收益"] == 1.5           # 1500/1000


def test_each_metric_carries_formula_and_input_fact_ids():
    output = run({"facts": BASE_FACTS, "metrics": ["roe"]})
    metric = output["data"][0]
    assert metric["formula"] == "净利润 ÷ 股东权益"
    assert metric["unit"] == "%"
    assert metric["input_fact_ids"] == ["净利润@FY2025", "股东权益@FY2025"]


def test_growth_uses_two_latest_periods():
    output = run({
        "facts": [
            *BASE_FACTS,
            fact("营收", 6000, "FY2023"),
        ],
        "metrics": ["growth"],
    })
    metric = output["data"][0]
    assert metric["value"] == 25.0
    assert metric["input_fact_ids"] == ["营收@FY2025", "营收@FY2024"]


def test_metric_name_aliases_are_equivalent():
    zh = run({"facts": BASE_FACTS, "metrics": ["毛利率"]})
    en = run({"facts": BASE_FACTS, "metrics": ["margin"]})
    assert values_by_name(zh) == values_by_name(en)


def test_metric_definitions_are_complete():
    # Every definition must expose a compute function and a display label.
    for key, definition in METRICS.items():
        assert definition.compute is not None
        assert definition.label
        assert definition.unit in {"%", "倍", "元"}
    assert resolve_metric("roe") is resolve_metric("净资产收益率")


# ---------------------------------------------------------------------------
# Missing / zero / malformed inputs never raise
# ---------------------------------------------------------------------------


def test_missing_facts_produce_degraded_reason():
    output = run({
        "facts": [fact("营收", 10000), fact("营业成本", 6000)],
        "metrics": ["margin", "roe"],
    })
    assert output["status"] == "ok"
    assert output["degraded"] is True
    assert "净资产收益率" in output["degraded_reason"]
    assert "毛利率" in values_by_name(output)


def test_all_missing_facts_return_empty():
    output = run({
        "facts": [fact("员工人数", 500)],
        "metrics": ["roe", "margin"],
    })
    assert output["status"] == "empty"
    assert output["degraded"] is True
    assert output["data"] == []


def test_division_by_zero_does_not_crash():
    output = run({
        "facts": [fact("营收", 0), fact("营业成本", 0), fact("净利润", 1), fact("股东权益", 0)],
        "metrics": ["margin", "roe"],
    })
    assert output["status"] == "empty"
    assert output["data"] == []


def test_unknown_metric_name_reported():
    output = run({"facts": BASE_FACTS, "metrics": ["not_a_metric"]})
    assert output["status"] == "empty"
    assert "unknown metric" in output["degraded_reason"]


def test_no_facts_returns_empty_with_honest_reason():
    output = run({"facts": [], "metrics": ["roe"]})
    assert output["status"] == "empty"
    assert "extract_financial_facts" in output["degraded_reason"]


def test_empty_metrics_list_returns_empty():
    output = run({"facts": BASE_FACTS, "metrics": []})
    assert output["status"] == "empty"


def test_string_values_are_parsed():
    output = run({
        "facts": [
            fact("营收", "10,000"),
            fact("营业成本", "6,000"),
        ],
        "metrics": ["margin"],
    })
    assert values_by_name(output)["毛利率"] == 40.0


def test_negative_profit_is_computed():
    output = run({
        "facts": [fact("净利润", -500, "FY2025"), fact("股东权益", 5000, "FY2025")],
        "metrics": ["roe"],
    })
    assert values_by_name(output)["净资产收益率"] == -10.0


def test_ratio_metrics_never_mix_periods():
    output = run({
        "facts": [
            fact("净利润", 1500, "FY2024"),
            fact("股东权益", 5000, "FY2025"),
        ],
        "metrics": ["roe"],
    })
    assert output["status"] == "empty"
    assert "净资产收益率" in output["degraded_reason"]


# ---------------------------------------------------------------------------
# Tool registry integration
# ---------------------------------------------------------------------------


def test_registry_wiring_returns_real_values():
    registry = build_default_registry()
    execution = asyncio.run(registry.execute(
        "calculate_financial_metrics",
        {"facts": BASE_FACTS, "metrics": ["roe", "margin"]},
    ))
    assert execution.output["status"] == "ok"
    assert len(execution.output["data"]) == 2
    names = {item["name"] for item in execution.output["data"]}
    assert names == {"净资产收益率", "毛利率"}
    # The unconfigured stub must never surface through this tool anymore.
    assert "unconfigured" not in str(execution.output)


def test_input_schema_rejects_extra_fields():
    registry = build_default_registry()
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute(
            "calculate_financial_metrics",
            {"facts": BASE_FACTS, "metrics": ["roe"], "evil": "injection"},
        ))


def test_input_schema_rejects_malformed_facts():
    registry = build_default_registry()
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute(
            "calculate_financial_metrics",
            {"metrics": ["roe"], "facts": [{"name": "净利润", "period": "FY2025"}]},
        ))


def test_fact_index_ignores_unparsable_values():
    index = build_index([
        CalculateFinancialMetricsInput.model_validate({
            "facts": [fact("营收", "n/a"), fact("净利润", 1500)],
            "metrics": [],
        }).facts[0],
        CalculateFinancialMetricsInput.model_validate({
            "facts": [fact("净利润", 1500)],
            "metrics": [],
        }).facts[0],
    ])
    assert "营收" not in index
    assert index["净利润"]["FY2025"] == 1500.0
