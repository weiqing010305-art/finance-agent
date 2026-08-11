from __future__ import annotations

import re
from statistics import mean
from typing import Any
from urllib.parse import urlparse


def _normalized_text(value: Any) -> str:
    return re.sub(r"[\s·・（）()\-_]+", "", str(value or "")).casefold()


def _company_matches(actual: Any, expected: Any) -> bool:
    actual_text = _normalized_text(actual)
    expected_text = _normalized_text(expected)
    return bool(
        actual_text
        and expected_text
        and (actual_text in expected_text or expected_text in actual_text)
    )


def _normalized_symbol(value: Any) -> str:
    symbol = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()
    if symbol.isdigit():
        return symbol.lstrip("0") or "0"
    return symbol


def _is_http_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def grade_task(
    case: dict[str, Any],
    task: dict[str, Any],
    *,
    first_result_seconds: float | None,
    total_seconds: float,
    url_reachability: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Grade only properties that are deterministically visible in a task response."""
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    sections = [item for item in result.get("sections", []) if isinstance(item, dict)]
    evidence = [item for item in task.get("evidence", []) if isinstance(item, dict)]

    evidence_numbers = {
        item.get("citation_number")
        for item in evidence
        if isinstance(item.get("citation_number"), int)
    }
    citation_references: list[int] = []
    cited_sections = 0
    for section in sections:
        citations = [
            number
            for number in section.get("citations", [])
            if isinstance(number, int)
        ]
        if citations:
            cited_sections += 1
            citation_references.extend(citations)

    valid_references = sum(number in evidence_numbers for number in citation_references)
    urls = [str(item.get("url") or "").strip() for item in evidence]
    syntactically_valid_urls = sum(_is_http_url(url) for url in urls)

    acceptable_symbols = {
        _normalized_symbol(value) for value in case.get("acceptable_symbols", [])
    }
    actual_symbol = _normalized_symbol(task.get("symbol") or result.get("symbol"))
    symbol_match = actual_symbol in acceptable_symbols if acceptable_symbols else not actual_symbol

    acceptable_markets = {
        str(value).strip().upper() for value in case.get("acceptable_markets", [])
    }
    actual_market = str(task.get("market") or result.get("market") or "").strip().upper()

    reachable_rate = None
    if url_reachability is not None:
        checked = [url_reachability[url] for url in urls if url in url_reachability]
        reachable_rate = _ratio(sum(checked), len(checked))

    return {
        "task_completed": task.get("status") == "completed",
        "company_match": _company_matches(
            task.get("company") or result.get("company"), case.get("expected_company")
        ),
        "symbol_match": symbol_match,
        "market_match": actual_market in acceptable_markets,
        "has_report": bool(result),
        "report_section_count": len(sections),
        "evidence_count": len(evidence),
        "section_citation_coverage": _ratio(cited_sections, len(sections)),
        "citation_reference_integrity": _ratio(valid_references, len(citation_references)),
        "evidence_url_syntax_rate": _ratio(syntactically_valid_urls, len(urls)),
        "evidence_url_reachability_rate": reachable_rate,
        "first_result_seconds": (
            round(first_result_seconds, 3) if first_result_seconds is not None else None
        ),
        "total_seconds": round(total_seconds, 3),
        "citation_semantic_support_rate": None,
        "token_usage": None,
        "estimated_cost": None,
    }

def failed_grade(total_seconds: float) -> dict[str, Any]:
    return {
        "task_completed": False,
        "company_match": False,
        "symbol_match": False,
        "market_match": False,
        "has_report": False,
        "report_section_count": 0,
        "evidence_count": 0,
        "section_citation_coverage": None,
        "citation_reference_integrity": None,
        "evidence_url_syntax_rate": None,
        "evidence_url_reachability_rate": None,
        "first_result_seconds": None,
        "total_seconds": round(total_seconds, 3),
        "citation_semantic_support_rate": None,
        "token_usage": None,
        "estimated_cost": None,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [item["metrics"] for item in results]

    def rate(name: str) -> float | None:
        values = [value for item in metrics if isinstance((value := item.get(name)), bool)]
        return round(sum(values) / len(values), 4) if values else None

    def average(name: str) -> float | None:
        values = [
            float(value)
            for item in metrics
            if isinstance((value := item.get(name)), (int, float))
            and not isinstance(value, bool)
        ]
        return round(mean(values), 4) if values else None

    return {
        "case_count": len(results),
        "task_completion_rate": rate("task_completed"),
        "company_match_rate": rate("company_match"),
        "symbol_match_rate": rate("symbol_match"),
        "market_match_rate": rate("market_match"),
        "report_rate": rate("has_report"),
        "average_section_citation_coverage": average("section_citation_coverage"),
        "average_citation_reference_integrity": average("citation_reference_integrity"),
        "average_evidence_url_syntax_rate": average("evidence_url_syntax_rate"),
        "average_evidence_url_reachability_rate": average(
            "evidence_url_reachability_rate"
        ),
        "average_first_result_seconds": average("first_result_seconds"),
        "average_total_seconds": average("total_seconds"),
        "unavailable_metrics": [
            "citation_semantic_support_rate",
            "token_usage",
            "estimated_cost",
        ],
    }
