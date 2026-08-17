import pytest
from pydantic import ValidationError

from backend.planner import DeterministicPlanner, PlannerError
from backend.schemas import PlanStep, ResearchPlan, SecurityCandidate


@pytest.fixture
def entity():
    return SecurityCandidate(
        candidate_id="HK:0700.HK", company="腾讯控股", symbol="0700.HK",
        market="HK", confidence=1.0, matched_alias="腾讯",
    )


@pytest.mark.parametrize("depth,min_steps", [("quick", 5), ("standard", 6), ("deep", 7)])
def test_planner_builds_valid_dynamic_dag_with_hybrid_retrieval(entity, depth, min_steps):
    plan = DeterministicPlanner().create_plan(
        question="分析腾讯盈利质量和现金流", entity=entity, depth=depth, budget_limit=30
    )
    assert len(plan.steps) >= min_steps
    retrieval = next(step for step in plan.steps if step.tool_name == "retrieve_documents")
    assert retrieval.input["retrieval_mode"] == "hybrid"
    assert retrieval.input["fusion"] == "rrf"
    assert plan.estimated_cost <= 30


@pytest.mark.parametrize(
    "steps,pattern",
    [
        ([PlanStep(id="same", tool_name="a", success_criteria=["ok"]), PlanStep(id="same", tool_name="b", success_criteria=["ok"])], "unique"),
        ([PlanStep(id="a", tool_name="a", dependencies=["missing"], success_criteria=["ok"])], "does not exist"),
        ([PlanStep(id="a", tool_name="a", dependencies=["b"], success_criteria=["ok"]), PlanStep(id="b", tool_name="b", dependencies=["a"], success_criteria=["ok"])], "cycle"),
    ],
)
def test_plan_schema_rejects_invalid_dags(steps, pattern):
    with pytest.raises(ValidationError, match=pattern):
        ResearchPlan(goal="valid research goal", steps=steps)


def test_planner_rejects_plan_over_budget(entity):
    with pytest.raises(PlannerError, match="exceeds budget"):
        DeterministicPlanner().create_plan(
            question="分析腾讯盈利质量", entity=entity, depth="deep", budget_limit=2
        )


def test_only_one_automatic_replan_is_allowed(entity):
    planner = DeterministicPlanner()
    plan = planner.create_plan(
        question="分析腾讯盈利质量", entity=entity, depth="quick", budget_limit=30
    )
    replanned = planner.replan(plan, reason="insufficient evidence", remaining_budget=30)
    assert replanned.version == plan.version + 1
    assert replanned.fallback_used is True
    with pytest.raises(PlannerError, match="limit"):
        planner.replan(replanned, reason="still insufficient", remaining_budget=30)
