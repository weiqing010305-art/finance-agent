from pathlib import Path

from evals.run_intent_routing import run_cases


CASES = Path("evals/intent-routing-cases.json")


def test_routing_eval_dataset_is_large_and_covers_safety_cases():
    metrics = run_cases(CASES)

    assert metrics["case_count"] >= 40
    assert metrics["accuracy"] >= 0.95, metrics["failures"]
    assert metrics["research_recall"] == 1.0
    assert metrics["false_planner_activation_rate"] == 0.0
    assert metrics["false_research_permission_rate"] == 0.0
    assert metrics["p95_latency_ms"] < 100
