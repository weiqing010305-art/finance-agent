from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
from time import perf_counter
from typing import Any

from backend.agent_graph import RoutingGraph
from backend.context_builder import CaseContext


RESEARCH_INTENTS = {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"}


@dataclass
class MutableContextBuilder:
    context: CaseContext = CaseContext()

    def build(self, case_id: str | None) -> CaseContext:
        if self.context.case_id != case_id:
            raise KeyError(case_id)
        return self.context


def _case_context(raw: dict[str, Any]) -> CaseContext:
    case_id = raw.get("case_id")
    active_status = raw.get("active_run_status")
    active_run = None
    if active_status:
        active_run = {
            "id": "eval-run",
            "status": active_status,
            "current_step": "eval",
            "progress": 50,
        }
    pending = None
    if raw.get("has_pending_confirmation"):
        pending = {"id": "eval-confirm", "kind": "eval", "prompt": "请确认"}
    return CaseContext(
        case_id=case_id,
        active_run=active_run,
        pending_confirmation=pending,
        has_report=bool(raw.get("has_report")),
        report_has_evidence=bool(raw.get("report_has_evidence")),
    )


def run_cases(path: str | Path) -> dict[str, Any]:
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    context_builder = MutableContextBuilder()
    graph = RoutingGraph(context_builder)  # type: ignore[arg-type]
    latencies: list[float] = []
    failures: list[dict[str, str]] = []
    correct = 0
    research_total = 0
    research_recalled = 0
    non_research_total = 0
    false_research_permission = 0
    false_planner_activation = 0
    clarification_count = 0

    for item in cases:
        context_builder.context = _case_context(item.get("context", {}))
        started = perf_counter()
        result = graph.route(item["message"], case_id=context_builder.context.case_id)
        latencies.append((perf_counter() - started) * 1_000)
        predicted = result["decision"]["intent"]
        expected = item["expected_intent"]
        if predicted == expected:
            correct += 1
        else:
            failures.append({"id": item["id"], "expected": expected, "predicted": predicted})
        if expected in RESEARCH_INTENTS:
            research_total += 1
            if predicted in RESEARCH_INTENTS:
                research_recalled += 1
        else:
            non_research_total += 1
            if result["decision"]["requires_planner"]:
                false_planner_activation += 1
            if result["decision"]["external_research_allowed"]:
                false_research_permission += 1
        if predicted in {"CLARIFICATION", "AMBIGUOUS"}:
            clarification_count += 1

    sorted_latency = sorted(latencies)
    p95_index = max(0, math.ceil(len(sorted_latency) * 0.95) - 1)
    return {
        "case_count": len(cases),
        "accuracy": correct / len(cases) if cases else 0.0,
        "research_recall": research_recalled / research_total if research_total else 1.0,
        "false_research_permission_rate": (
            false_research_permission / non_research_total if non_research_total else 0.0
        ),
        "false_planner_activation_rate": (
            false_planner_activation / non_research_total if non_research_total else 0.0
        ),
        "clarification_rate": clarification_count / len(cases) if cases else 0.0,
        "p95_latency_ms": sorted_latency[p95_index] if sorted_latency else 0.0,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        default=str(Path(__file__).with_name("intent-routing-cases.json")),
    )
    args = parser.parse_args()
    print(json.dumps(run_cases(args.path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
