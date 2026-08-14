import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner, RunConflict
from backend.planner import DeterministicPlanner
from backend.schemas import ResearchCreate, SecurityCandidate


def test_install_plan_is_versioned_idempotent_and_checkpoints_frontier(tmp_path):
    repository = Repository(tmp_path / "plan.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", symbol="0700.HK", market="HK", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="plan-run",
    )
    entity = SecurityCandidate(
        candidate_id="HK:0700.HK", company="腾讯控股", symbol="0700.HK",
        market="HK", confidence=1, matched_alias="腾讯",
    )
    plan = DeterministicPlanner().create_plan(
        question="分析腾讯盈利质量", entity=entity, depth="standard", budget_limit=30
    ).model_dump()

    snapshot = runner.install_plan(created.run["id"], lease_token=created.lease_token, plan=plan)
    replay = runner.install_plan(created.run["id"], lease_token=created.lease_token, plan=plan)

    assert snapshot["plan"]["version"] == 2
    assert replay["counts"] == snapshot["counts"]
    assert snapshot["checkpoint"]["frontier"]["plan_version"] == 2
    assert set(snapshot["checkpoint"]["frontier"]["ready_step_ids"]) == {
        "search_filings", "search_web", "retrieve_documents"
    }
    assert snapshot["events"][-1]["kind"] == "plan.created"


def test_plan_install_requires_current_lease_and_exact_next_version(tmp_path):
    repository = Repository(tmp_path / "invalid-plan.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="invalid-plan",
    )
    base = {
        "version": 3, "goal": "分析腾讯盈利质量", "max_replans": 1,
        "fallback_used": False,
        "steps": [{
            "id": "search", "kind": "tool", "tool_name": "search_web",
            "dependencies": [], "input": {}, "success_criteria": ["ok"],
            "max_attempts": 1, "estimated_cost": 1,
        }],
    }
    with pytest.raises(RunConflict, match="advance"):
        runner.install_plan(created.run["id"], lease_token=created.lease_token, plan=base)
    with pytest.raises(RunConflict, match="lease"):
        runner.install_plan(
            created.run["id"], lease_token="wrong", plan={**base, "version": 2}
        )


def test_repository_rejects_unknown_tool_and_invalid_dag(tmp_path):
    repository = Repository(tmp_path / "strict-plan.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="strict-plan",
    )
    invalid = {
        "version": 2, "goal": "分析腾讯盈利质量", "steps": [{
            "id": "bad", "kind": "tool", "tool_name": "delete_database",
            "dependencies": [], "input": {}, "success_criteria": ["bad"],
            "max_attempts": 1, "estimated_cost": 1,
        }],
    }
    with pytest.raises(RunConflict, match="unregistered"):
        runner.install_plan(created.run["id"], lease_token=created.lease_token, plan=invalid)
    invalid["steps"][0]["tool_name"] = "search_web"
    invalid["steps"][0]["dependencies"] = ["missing"]
    with pytest.raises(RunConflict, match="dependency"):
        runner.install_plan(created.run["id"], lease_token=created.lease_token, plan=invalid)
