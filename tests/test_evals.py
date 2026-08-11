from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from evals.graders import failed_grade, grade_task, summarize
from evals.run_eval import load_cases, run_case


CASES = Path(__file__).parents[1] / "evals" / "cases.jsonl"


def sample_case() -> dict:
    return {
        "id": "hk_tencent",
        "category": "hk",
        "question": "腾讯控股的盈利驱动是什么？",
        "expected_company": "腾讯控股",
        "acceptable_symbols": ["00700", "0700"],
        "acceptable_markets": ["HK"],
        "expected_behavior": "引用公开披露。",
    }


def sample_task(status: str = "completed") -> dict:
    return {
        "id": "task-1",
        "status": status,
        "company": "腾讯控股有限公司",
        "symbol": "700",
        "market": "HK",
        "error": None,
        "result": {
            "provider": "test",
            "sections": [
                {"title": "盈利驱动", "content": "广告收入增长。", "citations": [1]},
                {"title": "风险", "content": "竞争仍然激烈。", "citations": [2]},
            ],
        },
        "evidence": [
            {"citation_number": 1, "url": "https://example.com/one"},
            {"citation_number": 2, "url": "https://example.com/two"},
        ],
    }


def test_case_file_has_ten_unique_scenarios() -> None:
    cases = load_cases(CASES)

    assert len(cases) == 10
    assert len({case["id"] for case in cases}) == 10
    assert {case["category"] for case in cases} == {
        "cn",
        "hk",
        "ambiguous",
        "insufficient",
        "conflict",
    }


def test_grade_task_scores_identity_and_citation_integrity() -> None:
    metrics = grade_task(
        sample_case(),
        sample_task(),
        first_result_seconds=2.3456,
        total_seconds=4.5678,
        url_reachability={
            "https://example.com/one": True,
            "https://example.com/two": False,
        },
    )

    assert metrics["task_completed"] is True
    assert metrics["company_match"] is True
    assert metrics["symbol_match"] is True
    assert metrics["market_match"] is True
    assert metrics["section_citation_coverage"] == 1.0
    assert metrics["citation_reference_integrity"] == 1.0
    assert metrics["evidence_url_syntax_rate"] == 1.0
    assert metrics["evidence_url_reachability_rate"] == 0.5
    assert metrics["first_result_seconds"] == 2.346
    assert metrics["citation_semantic_support_rate"] is None


def test_grade_task_detects_missing_and_broken_citations() -> None:
    task = sample_task()
    task["result"]["sections"][0]["citations"] = [99]
    task["result"]["sections"][1]["citations"] = []
    task["evidence"][1]["url"] = "not-a-url"

    metrics = grade_task(
        sample_case(), task, first_result_seconds=None, total_seconds=1.0
    )

    assert metrics["section_citation_coverage"] == 0.5
    assert metrics["citation_reference_integrity"] == 0.0
    assert metrics["evidence_url_syntax_rate"] == 0.5


def test_summary_ignores_unavailable_metrics() -> None:
    passed = grade_task(
        sample_case(), sample_task(), first_result_seconds=1.0, total_seconds=2.0
    )
    report = summarize(
        [
            {"metrics": passed},
            {"metrics": failed_grade(3.0)},
        ]
    )

    assert report["case_count"] == 2
    assert report["task_completion_rate"] == 0.5
    assert report["average_total_seconds"] == 2.5
    assert "citation_semantic_support_rate" in report["unavailable_metrics"]


def test_run_case_exercises_api_until_terminal() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            return httpx.Response(202, json={**sample_task("queued"), "result": None, "evidence": []})
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={**sample_task("running")})
        return httpx.Response(200, json=sample_task())

    with httpx.Client(
        base_url="http://test/api", transport=httpx.MockTransport(handler)
    ) as client:
        result = run_case(
            client,
            sample_case(),
            timeout_seconds=1.0,
            poll_seconds=0.001,
            should_check_urls=False,
        )

    assert calls == 2
    assert result["status"] == "completed"
    assert result["metrics"]["task_completed"] is True
    assert result["metrics"]["first_result_seconds"] is not None


def test_load_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    duplicate = '{"id":"same","category":"cn","question":"valid question","expected_company":"x","acceptable_symbols":[],"acceptable_markets":["CN"],"expected_behavior":"review"}'
    path = tmp_path / "cases.jsonl"
    path.write_text(f"{duplicate}\n{duplicate}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate case id"):
        load_cases(path)
