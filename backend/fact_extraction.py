"""Deterministic extraction of financial facts from document text.

``FactExtractor`` is a rules-based extractor: it finds statement line items
(using the same alias table as the metrics calculator), pairs them with
amounts and periods inside the same sentence, and binds every fact to its
source document. No model is involved, so extraction is deterministic,
auditable and cheap.

Rules are intentionally conservative: a fact is only emitted when a known
line item, a numeric amount with a unit, and a period all appear together.
Missing inputs degrade to ``empty`` with an explicit reason.
"""

from __future__ import annotations

import re
from typing import Any

from backend.financial_metrics import FACT_ALIASES
from backend.tool_registry import (
    DocumentTextInput,
    ExtractFinancialFactsInput,
    FinancialFact,
)

# Longest alias first so "归属于母公司股东的净利润" wins over "净利润".
_ALIAS_MATCHERS: list[tuple[str, str]] = sorted(
    (
        (canonical, alias)
        for canonical, aliases in FACT_ALIASES.items()
        for alias in aliases
    ),
    key=lambda item: len(item[1]),
    reverse=True,
)

_PERIOD_PATTERN = re.compile(r"\b(FY\s*)?(20\d{2})\b")
_AMOUNT_PATTERN = re.compile(
    r"([-+]?\d[\d,]*(?:\.\d+)?)\s*(万亿|亿|万|千|百)?\s*(美元|港币|港元|人民币|元)?"
)
_CURRENCIES = {"美元": "USD", "港币": "HKD", "港元": "HKD", "人民币": "CNY"}

_SENTENCE_SPLIT = re.compile(r"[。；;！!\n]+")


def _normalize_number(raw: str) -> float:
    return float(raw.replace(",", "").replace("，", ""))


def _find_period(text: str) -> str | None:
    match = _PERIOD_PATTERN.search(text)
    if match is None:
        return None
    year = match.group(2)
    return f"FY{year}" if match.group(1) else year


def _find_amount(text: str) -> tuple[float, str, str] | None:
    """Return (value, unit, currency) for the first amount-like token.

    Bare numbers without a unit (e.g. a year "2025" before the actual amount)
    are skipped so the search continues to the real amount.
    """
    for match in _AMOUNT_PATTERN.finditer(text):
        magnitude = match.group(2)
        currency_word = match.group(3)
        if magnitude is None and currency_word is None:
            continue  # bare number, not a financial amount
        value = _normalize_number(match.group(1))
        unit = (magnitude or "") + (currency_word or "元")
        currency = _CURRENCIES.get(currency_word or "", "CNY") if currency_word else "CNY"
        return value, unit, currency
    return None


def _find_line_item(text: str) -> tuple[str, str] | None:
    """Return (canonical name, matched alias) for the first known line item."""
    for canonical, alias in _ALIAS_MATCHERS:
        if alias in text:
            return canonical, alias
    return None


def _line_items_in_order(text: str) -> list[tuple[int, str, str]]:
    """All known line items with their positions, longest alias first."""
    matches: list[tuple[int, str, str]] = []
    for canonical, alias in _ALIAS_MATCHERS:
        for match in re.finditer(re.escape(alias), text):
            matches.append((match.start(), canonical, alias))
    matches.sort(key=lambda item: item[0])
    return matches


def _extract_sentence_facts(
    sentence: str,
    *,
    period: str | None,
    source_id: str,
    seen: set[tuple[str, float, str, str]],
    facts: list[FinancialFact],
    max_facts: int,
) -> None:
    """Extract every line item + amount pair from one sentence.

    Each line item claims the window from its own position to the next line
    item, so "营业收入 100 亿元，净利润 15 亿元" yields two facts instead of
    only the first.
    """
    if period is None:
        return
    items = _line_items_in_order(sentence)
    for index, (position, canonical, _alias) in enumerate(items):
        window_end = items[index + 1][0] if index + 1 < len(items) else len(sentence)
        amount = _find_amount(sentence[position:window_end])
        if amount is None:
            continue
        value, unit, currency = amount
        identity = (canonical, value, period, source_id)
        if identity in seen:
            continue
        seen.add(identity)
        facts.append(FinancialFact(
            name=canonical,
            value=value,
            period=period,
            unit=unit,
            currency=currency,
            source_id=source_id,
        ))
        if len(facts) >= max_facts:
            return


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


class FactExtractor:
    def __init__(self, max_facts: int = 200) -> None:
        self.max_facts = max_facts

    def extract(
        self,
        texts: list[DocumentTextInput],
        *,
        periods: int = 3,
    ) -> list[FinancialFact]:
        facts: list[FinancialFact] = []
        seen: set[tuple[str, float, str, str]] = set()
        for document in texts:
            fallback_period = _find_period(document.text)
            for sentence in split_sentences(document.text):
                _extract_sentence_facts(
                    sentence,
                    period=_find_period(sentence) or fallback_period,
                    source_id=document.source_id,
                    seen=seen,
                    facts=facts,
                    max_facts=self.max_facts,
                )
                if len(facts) >= self.max_facts:
                    return self._keep_recent_periods(facts, periods)
        return self._keep_recent_periods(facts, periods)

    @staticmethod
    def _keep_recent_periods(
        facts: list[FinancialFact],
        periods: int,
    ) -> list[FinancialFact]:
        """Keep only the N most recent distinct periods (periods input)."""
        ordered: dict[str, list[FinancialFact]] = {}
        for fact in facts:
            ordered.setdefault(fact.period, []).append(fact)

        def period_sort_key(period: str) -> tuple[int, str]:
            match = _PERIOD_PATTERN.search(period)
            year = int(match.group(2)) if match else 0
            return (year, period)

        recent = sorted(ordered.keys(), key=period_sort_key, reverse=True)[:periods]
        kept: list[FinancialFact] = []
        for period in recent:
            kept.extend(ordered[period])
        return kept


async def extract_financial_facts(
    payload: ExtractFinancialFactsInput,
    context: Any = None,
) -> dict[str, Any]:
    if not payload.texts:
        return {
            "status": "empty",
            "data": [],
            "evidence": [],
            "degraded": True,
            "degraded_reason": "no document texts supplied; run read_document or search_filings first",
            "fallback_used": None,
        }
    extractor = FactExtractor()
    facts = extractor.extract(payload.texts, periods=payload.periods)
    if not facts:
        return {
            "status": "empty",
            "data": [],
            "evidence": [],
            "degraded": True,
            "degraded_reason": "no financial facts with period, amount and unit found in texts",
            "fallback_used": None,
        }
    return {
        "status": "ok",
        "data": [fact.model_dump() for fact in facts],
        "evidence": [],
        "degraded": False,
        "degraded_reason": None,
        "fallback_used": None,
    }
