"""Deterministic financial metric calculation.

This module turns raw financial statement line items (facts) into metrics
using pure Python formulas. No model is involved in the arithmetic: the LLM
only chooses which metrics to request and which facts to supply. Every result
carries its formula and the exact input facts it consumed, so a downstream
reporter can trace each number back to source line items.

Missing or unusable inputs never raise: the affected metric is reported as a
degraded reason and the tool degrades to ``empty`` when nothing can be
computed (see ``calculate_financial_metrics``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from backend.tool_registry import (
    CalculateFinancialMetricsInput,
    FinancialFactInput,
    FinancialMetric,
)

# ---------------------------------------------------------------------------
# Statement line-item aliases
# ---------------------------------------------------------------------------
# Canonical key -> accepted names (Chinese first, then English). The first
# alias is the display name used in formulas and output.
FACT_ALIASES: dict[str, tuple[str, ...]] = {
    "营收": ("营收", "营业收入", "营业收入净额", "revenue", "operating revenue", "turnover"),
    "营业成本": ("营业成本", "cost of revenue", "cost of goods sold", "cogs"),
    "净利润": (
        "净利润",
        "归母净利润",
        "归属于母公司股东的净利润",
        "net profit",
        "net income",
        "profit attributable to owners",
    ),
    "经营现金流": (
        "经营活动现金流量净额",
        "经营现金流",
        "经营性现金流",
        "operating cash flow",
        "ocf",
        "cash flow from operations",
    ),
    "总资产": ("总资产", "资产总计", "total assets"),
    "总负债": ("总负债", "负债合计", "total liabilities"),
    "股东权益": (
        "股东权益",
        "所有者权益",
        "净资产",
        "归属于母公司股东权益",
        "equity",
        "shareholders equity",
        "net assets",
    ),
    "流动资产": ("流动资产", "流动资产合计", "current assets"),
    "流动负债": ("流动负债", "流动负债合计", "current liabilities"),
    "股本": ("股本", "总股本", "加权平均股本", "shares", "share count", "weighted average shares"),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical
    for canonical, aliases in FACT_ALIASES.items()
    for alias in aliases
}

# ---------------------------------------------------------------------------
# Facts index
# ---------------------------------------------------------------------------
# canonical name -> {period: numeric value}
FactsIndex = dict[str, dict[str, float]]


def _to_number(value: float | int | str) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("，", "").strip().rstrip("%")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _period_year(period: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", period)
    return int(match.group(0)) if match else None


def _period_key(period: str) -> tuple[int, str]:
    """Sort periods newest-first: numeric year wins, strings fall back."""
    year = _period_year(period)
    return (year if year is not None else 0, period)


def build_index(facts: list[FinancialFactInput]) -> FactsIndex:
    index: FactsIndex = {}
    for fact in facts:
        canonical = _ALIAS_TO_CANONICAL.get(fact.name.strip(), fact.name.strip())
        value = _to_number(fact.value)
        if value is None:
            continue
        index.setdefault(canonical, {})[fact.period] = value
    return index


def latest(facts: FactsIndex, canonical: str) -> tuple[str, float] | None:
    """Return the (period, value) of the most recent period for a line item."""
    by_period = facts.get(canonical)
    if not by_period:
        return None
    period = max(by_period, key=_period_key)
    return period, by_period[period]


def latest_two(facts: FactsIndex, canonical: str) -> tuple[tuple[str, float], tuple[str, float]] | None:
    """Return the two most recent (period, value) pairs for a line item."""
    by_period = facts.get(canonical)
    if not by_period or len(by_period) < 2:
        return None
    ordered = sorted(by_period.items(), key=lambda item: _period_key(item[0]), reverse=True)
    return (ordered[0][0], ordered[0][1]), (ordered[1][0], ordered[1][1])


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricDef:
    names: tuple[str, ...]
    label: str
    unit: str
    compute: Callable[[FactsIndex], tuple[float, str, list[str]] | None]


def _same_period(
    facts: FactsIndex, primary: str, secondary: str,
) -> tuple[tuple[str, float], tuple[str, float]] | None:
    """Pair two line items on the SAME period (primary's latest period).

    Ratio metrics must never mix periods (e.g. FY2024 net profit over FY2025
    equity), so the denominator is only accepted if it exists for the exact
    period of the numerator.
    """
    first = latest(facts, primary)
    if first is None:
        return None
    by_period = facts.get(secondary)
    if by_period is None or first[0] not in by_period:
        return None
    return first, (first[0], by_period[first[0]])


def _ratio(
    facts: FactsIndex,
    numerator: str,
    denominator: str,
    *,
    percent: bool = True,
    label: str,
    formula: str,
) -> tuple[float, str, list[str]] | None:
    pair = _same_period(facts, numerator, denominator)
    if pair is None or pair[1][1] == 0:
        return None
    (num_period, num_value), (den_period, den_value) = pair
    value = (num_value / den_value) * (100.0 if percent else 1.0)
    return round(value, 4), formula, [f"{numerator}@{num_period}", f"{denominator}@{den_period}"]


def _gross_margin(facts: FactsIndex) -> tuple[float, str, list[str]] | None:
    pair = _same_period(facts, "营收", "营业成本")
    if pair is None or pair[0][1] == 0:
        return None
    (period, revenue), (_period, cost) = pair
    value = ((revenue - cost) / revenue) * 100.0
    return (
        round(value, 4),
        "(营收 - 营业成本) ÷ 营收",
        [f"营收@{period}", f"营业成本@{period}"],
    )


def _growth(facts: FactsIndex) -> tuple[float, str, list[str]] | None:
    pair = latest_two(facts, "营收")
    if pair is None or pair[1][1] == 0:
        return None
    (new_period, new_value), (old_period, old_value) = pair
    value = ((new_value - old_value) / old_value) * 100.0
    return (
        round(value, 4),
        f"营收增速 {new_period} vs {old_period}",
        [f"营收@{new_period}", f"营收@{old_period}"],
    )


METRICS: dict[str, MetricDef] = {
    "growth": MetricDef(
        names=("营收增速", "收入增速", "营业收入增速", "growth"),
        label="营收增速",
        unit="%",
        compute=_growth,
    ),
    "margin": MetricDef(
        names=("毛利率", "margin", "gross margin"),
        label="毛利率",
        unit="%",
        compute=_gross_margin,
    ),
    "net_margin": MetricDef(
        names=("净利率", "net_margin", "net margin", "net profit margin"),
        label="净利率",
        unit="%",
        compute=lambda f: _ratio(
            f, "净利润", "营收", percent=True, label="净利率",
            formula="净利润 ÷ 营收",
        ),
    ),
    "roe": MetricDef(
        names=("净资产收益率", "股东权益回报率", "roe", "return on equity"),
        label="净资产收益率",
        unit="%",
        compute=lambda f: _ratio(
            f, "净利润", "股东权益", percent=True, label="净资产收益率",
            formula="净利润 ÷ 股东权益",
        ),
    ),
    "roa": MetricDef(
        names=("总资产收益率", "资产回报率", "roa", "return on assets"),
        label="总资产收益率",
        unit="%",
        compute=lambda f: _ratio(
            f, "净利润", "总资产", percent=True, label="总资产收益率",
            formula="净利润 ÷ 总资产",
        ),
    ),
    "debt_ratio": MetricDef(
        names=("资产负债率", "debt_ratio", "debt ratio", "debt to assets"),
        label="资产负债率",
        unit="%",
        compute=lambda f: _ratio(
            f, "总负债", "总资产", percent=True, label="资产负债率",
            formula="总负债 ÷ 总资产",
        ),
    ),
    "current_ratio": MetricDef(
        names=("流动比率", "current_ratio", "current ratio"),
        label="流动比率",
        unit="倍",
        compute=lambda f: _ratio(
            f, "流动资产", "流动负债", percent=False, label="流动比率",
            formula="流动资产 ÷ 流动负债",
        ),
    ),
    "asset_turnover": MetricDef(
        names=("总资产周转率", "asset_turnover", "asset turnover"),
        label="总资产周转率",
        unit="倍",
        compute=lambda f: _ratio(
            f, "营收", "总资产", percent=False, label="总资产周转率",
            formula="营收 ÷ 总资产",
        ),
    ),
    "eps": MetricDef(
        names=("每股收益", "eps", "earnings per share"),
        label="每股收益",
        unit="元",
        compute=lambda f: _ratio(
            f, "净利润", "股本", percent=False, label="每股收益",
            formula="净利润 ÷ 股本",
        ),
    ),
    "cash_conversion": MetricDef(
        names=("现金转换率", "cash_conversion", "cash conversion", "cash conversion ratio"),
        label="现金转换率",
        unit="%",
        compute=lambda f: _ratio(
            f, "经营现金流", "净利润", percent=True, label="现金转换率",
            formula="经营现金流 ÷ 净利润",
        ),
    ),
}

_METRIC_BY_NAME: dict[str, MetricDef] = {
    alias: definition
    for definition in METRICS.values()
    for alias in definition.names
}


def resolve_metric(name: str) -> MetricDef | None:
    return _METRIC_BY_NAME.get(name.strip().lower())


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------
def _empty_result(reason: str) -> dict[str, Any]:
    return {
        "status": "empty",
        "data": [],
        "degraded": True,
        "degraded_reason": reason[:500],
        "fallback_used": None,
    }


async def calculate_financial_metrics(
    payload: CalculateFinancialMetricsInput,
    context: Any = None,
) -> dict[str, Any]:
    """Compute requested metrics deterministically from supplied facts.

    The handler never raises for missing or zero-valued inputs: uncomputable
    metrics are disclosed via ``degraded_reason`` and the result degrades to
    ``empty`` when nothing could be computed. Returning ``empty`` (instead of
    ``insufficient``) keeps the research executor's replan gate semantics:
    a stub extract step must not cascade into a failed run.
    """
    if not payload.facts:
        return _empty_result("no financial facts supplied; run extract_financial_facts first")
    if not payload.metrics:
        return _empty_result("no metrics requested")

    index = build_index(payload.facts)
    results: list[FinancialMetric] = []
    degraded_reasons: list[str] = []
    for requested in payload.metrics:
        definition = resolve_metric(requested)
        if definition is None:
            degraded_reasons.append(f"{requested}: unknown metric")
            continue
        computed = definition.compute(index)
        if computed is None:
            degraded_reasons.append(f"{definition.label}: missing required facts")
            continue
        value, formula, input_ids = computed
        results.append(FinancialMetric(
            name=definition.label,
            value=value,
            formula=formula,
            unit=definition.unit,
            input_fact_ids=input_ids,
        ))

    if not results:
        return _empty_result(
            "; ".join(degraded_reasons) or "no computable metrics from supplied facts"
        )
    return {
        "status": "ok",
        "data": [metric.model_dump() for metric in results],
        "degraded": bool(degraded_reasons),
        "degraded_reason": "; ".join(degraded_reasons) if degraded_reasons else None,
        "fallback_used": None,
    }
