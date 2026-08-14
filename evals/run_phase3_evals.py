from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any

from backend.entity_resolver import EntityResolver
from backend.planner import DeterministicPlanner
from backend.schemas import ResearchPlan, SecurityCandidate


CASES = Path(__file__).with_name("entity-resolution-cases.json")


def evaluate(cases_path: Path = CASES) -> dict[str, Any]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    resolver = EntityResolver()
    failures = []
    latencies = []
    ambiguous_total = ambiguous_safe = 0
    for case in cases:
        started = perf_counter()
        result = resolver.resolve(case["message"])
        latencies.append((perf_counter() - started) * 1_000)
        actual_symbols = sorted(item.symbol for item in result.candidates)
        expected_symbols = sorted(case["expected_symbols"])
        if result.status != case["expected_status"] or actual_symbols != expected_symbols:
            failures.append({
                "id": case["id"], "expected_status": case["expected_status"],
                "actual_status": result.status, "expected_symbols": expected_symbols,
                "actual_symbols": actual_symbols,
            })
        if case["expected_status"] == "ambiguous":
            ambiguous_total += 1
            ambiguous_safe += int(result.status == "ambiguous" and result.selected is None)

    entity = SecurityCandidate(
        candidate_id="HK:0700.HK", company="腾讯控股", symbol="0700.HK",
        market="HK", confidence=1, matched_alias="腾讯",
    )
    plans = [
        DeterministicPlanner().create_plan(
            question="分析腾讯盈利质量", entity=entity, depth=depth, budget_limit=30
        )
        for depth in ("quick", "standard", "deep")
    ]
    sorted_latency = sorted(latencies)
    p95_index = max(0, min(len(sorted_latency) - 1, int(len(sorted_latency) * 0.95) - 1))
    return {
        "entity_case_count": len(cases),
        "entity_accuracy": (len(cases) - len(failures)) / len(cases),
        "ambiguity_safety_rate": ambiguous_safe / ambiguous_total if ambiguous_total else 1.0,
        "entity_p95_latency_ms": sorted_latency[p95_index],
        "planner_smoke_case_count": len(plans),
        "schema_dag_validation_rate": sum(
            ResearchPlan.model_validate(plan.model_dump()) is not None for plan in plans
        ) / len(plans),
        "hybrid_plan_contract_smoke_rate": sum(
            any(
                step.tool_name == "retrieve_documents"
                and step.input.get("retrieval_mode") == "hybrid"
                and step.input.get("fusion") == "rrf"
                for step in plan.steps
            )
            for plan in plans
        ) / len(plans),
        "failures": failures,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
