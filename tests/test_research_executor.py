import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner
from backend.planner import DeterministicPlanner
from backend.policy import PolicyGate
from backend.research_executor import ExecutionDenied, ResearchExecutor
from backend.schemas import ResearchCreate, RouteDecision, SecurityCandidate
from backend.tool_registry import ToolRegistry, ToolResult, ToolSpec, GenericToolInput


def route():
    return RouteDecision(
        intent="RESEARCH_NEW", confidence=1, requires_planner=True,
        external_research_allowed=False, response_policy="await_entity_resolution",
    )


def build_executor(tmp_path, *, tracker=None):
    repository = Repository(tmp_path / "executor.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", symbol="0700.HK", market="HK", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="executor-run",
    )
    entity = SecurityCandidate(
        candidate_id="HK:0700.HK", company="腾讯控股", symbol="0700.HK",
        market="HK", confidence=1, matched_alias="腾讯",
    )
    plan = DeterministicPlanner().create_plan(
        question="分析腾讯盈利质量", entity=entity, depth="quick", budget_limit=30
    )
    runner.install_plan(created.run["id"], lease_token=created.lease_token, plan=plan.model_dump())
    registry = ToolRegistry()

    async def handler(payload):
        if tracker is not None:
            tracker.append(payload.model_dump())
        await asyncio.sleep(0.001)
        return {"status": "ok", "data": {"source": payload.company}, "evidence": []}

    for name in {step.tool_name for step in plan.steps}:
        registry.register(ToolSpec(
            name=name, version="1", risk_level="low", timeout_seconds=1,
            max_retries=1, idempotent=True, cost_class=1,
            requires_confirmation=False, input_model=GenericToolInput,
            output_model=ToolResult,
        ), handler)
    executor = ResearchExecutor(runner, registry, PolicyGate(repository, registry))
    return repository, runner, created, plan, executor


def test_executor_runs_ready_frontier_and_commits_each_checkpoint(tmp_path):
    repository, _runner, created, plan, executor = build_executor(tmp_path)
    result = asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=30,
    ))
    assert set(result.executed_step_ids) == {"search_filings", "retrieve_documents", "get_quote"}
    snapshot = repository.get_runtime_snapshot(created.run["id"])
    assert snapshot["counts"]["steps"] == 3
    assert snapshot["counts"]["tool_calls"] == 3
    assert snapshot["run"]["budget_used"] == 5
    assert "extract_facts" in snapshot["checkpoint"]["frontier"]["ready_step_ids"]


def test_executor_recovery_does_not_repeat_committed_tools(tmp_path):
    calls = []
    repository, _runner, created, _plan, executor = build_executor(tmp_path, tracker=calls)
    first = asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=30,
    ))
    call_count = len(calls)
    second = asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=30,
    ))
    assert len(calls) > call_count
    assert not set(first.executed_step_ids) & set(second.executed_step_ids)
    snapshot = repository.get_runtime_snapshot(created.run["id"])
    assert len({step["id"] for step in snapshot["steps"]}) == snapshot["counts"]["steps"]


def test_ready_frontier_budget_is_reserved_as_a_batch(tmp_path):
    _repository, _runner, created, _plan, executor = build_executor(tmp_path)
    with pytest.raises(ExecutionDenied, match="frontier"):
        asyncio.run(executor.execute_ready_batch(
            created.run["id"], lease_token=created.lease_token,
            route=route(), entity_confirmed=True, budget_limit=3,
        ))


def test_executor_stops_at_pause_checkpoint(tmp_path):
    repository, runner, created, _plan, executor = build_executor(tmp_path)
    runner.request_pause(created.run["id"])
    result = asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=30,
    ))
    assert result.executed_step_ids == []
    assert result.status == "pause_requested"
    assert repository.get_task(created.run["id"])["status"] == "pause_requested"


def test_executor_allows_only_one_replan(tmp_path):
    repository, _runner, created, plan, executor = build_executor(tmp_path)
    replanned = executor.replan_once(
        created.run["id"], lease_token=created.lease_token,
        reason="insufficient evidence", budget_limit=30,
    )
    assert replanned.version == plan.version + 1
    assert repository.get_runtime_snapshot(created.run["id"])["plan"]["fallback_used"] is True
    with pytest.raises(ExecutionDenied, match="limit"):
        executor.replan_once(
            created.run["id"], lease_token=created.lease_token,
            reason="still insufficient", budget_limit=30,
        )


def test_insufficient_observation_triggers_one_replan_before_commit(tmp_path):
    repository = Repository(tmp_path / "insufficient.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", symbol="0700.HK", market="HK", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="insufficient-run",
    )
    entity = SecurityCandidate(
        candidate_id="HK:0700.HK", company="腾讯控股", symbol="0700.HK",
        market="HK", confidence=1, matched_alias="腾讯",
    )
    plan = DeterministicPlanner().create_plan(
        question="分析腾讯盈利质量", entity=entity, depth="quick", budget_limit=30
    )
    runner.install_plan(created.run["id"], lease_token=created.lease_token, plan=plan.model_dump())
    registry = ToolRegistry()
    for name in {step.tool_name for step in plan.steps} | {"search_web"}:
        registry.register(ToolSpec(
            name=name, version="1", risk_level="low", timeout_seconds=1,
            max_retries=1, idempotent=True, cost_class=1,
            requires_confirmation=False, input_model=GenericToolInput,
            output_model=ToolResult,
        ), lambda _payload: {"status": "insufficient", "data": {}, "evidence": []})
    executor = ResearchExecutor(runner, registry, PolicyGate(repository, registry))
    result = asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=30,
    ))
    assert result.replanned is True
    snapshot = repository.get_runtime_snapshot(created.run["id"])
    assert snapshot["plan"]["version"] == plan.version + 1
    assert snapshot["counts"]["steps"] == 0
    with pytest.raises(ExecutionDenied, match="after replan"):
        asyncio.run(executor.execute_ready_batch(
            created.run["id"], lease_token=created.lease_token,
            route=route(), entity_confirmed=True, budget_limit=30,
        ))


def test_observed_claim_is_reused_without_repeating_external_tool(tmp_path):
    calls = []
    repository, _runner, created, plan, executor = build_executor(tmp_path, tracker=calls)
    step = next(item for item in plan.steps if item.id == "search_filings")
    decision = executor.policy.authorize(
        route=route(), run=repository.get_task(created.run["id"]), entity_confirmed=True,
        plan_version=plan.version, step=step, budget_limit=30,
    )
    claim = repository.claim_tool_execution(
        run_id=created.run["id"], plan_version=plan.version, step_id=step.id,
        tool_name=step.tool_name, lease_token=created.lease_token,
        capability_token=decision.capability_token, idempotency_key=f"tool:{plan.version}:{step.id}",
        step_input=step.input,
    )
    repository.record_tool_observation(
        run_id=created.run["id"], plan_version=plan.version, step_id=step.id,
        lease_token=created.lease_token, execution_token=claim["execution_token"],
        output={"status": "ok", "data": {"recovered": True}, "evidence": []},
        duration_ms=1,
    )
    asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=30,
    ))
    assert all(item.get("company") != step.input.get("company") for item in calls) is False
    assert sum(item.get("company") == step.input.get("company") for item in calls) == 1
    assert repository.get_completed_step_output(
        created.run["id"], f"plan:{plan.version}:step:{step.id}"
    )["data"]["recovered"] is True


def test_concurrent_executor_has_single_tool_claim_owner(tmp_path):
    calls = []
    repository, _runner, created, _plan, executor = build_executor(tmp_path, tracker=calls)

    def execute():
        try:
            return asyncio.run(executor.execute_ready_batch(
                created.run["id"], lease_token=created.lease_token,
                route=route(), entity_confirmed=True, budget_limit=30,
            ))
        except (ExecutionDenied, Exception):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _item: execute(), range(2)))
    with repository.connect() as connection:
        claims = connection.execute("SELECT COUNT(*) FROM tool_execution_claims").fetchone()[0]
    assert claims == 3
    assert len(calls) == 3


def test_executor_heartbeats_during_slow_tool_batch(tmp_path):
    repository = Repository(tmp_path / "heartbeat.db")
    repository.initialize()
    runner = DurableRunner(repository, lease_ttl=timedelta(milliseconds=300))
    entity = SecurityCandidate(
        candidate_id="HK:0700.HK", company="腾讯控股", symbol="0700.HK",
        market="HK", confidence=1, matched_alias="腾讯",
    )
    plan = DeterministicPlanner().create_plan(
        question="分析腾讯盈利质量", entity=entity, depth="quick", budget_limit=30,
        version=1,
    )
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", symbol="0700.HK", market="HK", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="heartbeat", initial_plan=plan.model_dump(),
    )
    registry = ToolRegistry()

    async def slow(_payload):
        await asyncio.sleep(0.5)
        return {"status": "ok", "data": {}, "evidence": []}

    for name in {step.tool_name for step in plan.steps}:
        registry.register(ToolSpec(
            name=name, version="1", risk_level="low", timeout_seconds=1,
            max_retries=0, idempotent=True, cost_class=1,
            requires_confirmation=False, input_model=GenericToolInput,
            output_model=ToolResult,
        ), slow)
    executor = ResearchExecutor(runner, registry, PolicyGate(repository, registry))
    result = asyncio.run(executor.execute_ready_batch(
        created.run["id"], lease_token=created.lease_token,
        route=route(), entity_confirmed=True, budget_limit=30,
    ))
    assert len(result.executed_step_ids) == 3


def test_phase3_tool_step_commit_cannot_bypass_or_forge_authorized_observation(tmp_path):
    repository, runner, created, plan, executor = build_executor(tmp_path)
    step = next(item for item in plan.steps if item.id == "search_filings")
    completed = {step.id}
    frontier = executor._frontier(plan, completed)
    with pytest.raises(Exception, match="authorized tool observation"):
        runner.commit_step(
            created.run["id"], lease_token=created.lease_token, step_id=step.id,
            kind=step.kind, step_input=step.input, step_output={"status": "ok"},
            idempotency_key=f"plan:{plan.version}:step:{step.id}", frontier=frontier,
            progress=20, budget_delta=step.estimated_cost, tool=None,
        )
    assert repository.get_runtime_snapshot(created.run["id"])["counts"]["steps"] == 0

    decision = executor.policy.authorize(
        route=route(), run=repository.get_task(created.run["id"]), entity_confirmed=True,
        plan_version=plan.version, step=step, budget_limit=30,
    )
    claim = repository.claim_tool_execution(
        run_id=created.run["id"], plan_version=plan.version, step_id=step.id,
        tool_name=step.tool_name, lease_token=created.lease_token,
        capability_token=decision.capability_token, idempotency_key=f"tool:{plan.version}:{step.id}",
        step_input=step.input,
    )
    observation = {"status": "ok", "data": {"real": True}, "evidence": []}
    repository.record_tool_observation(
        run_id=created.run["id"], plan_version=plan.version, step_id=step.id,
        lease_token=created.lease_token, execution_token=claim["execution_token"],
        output=observation, duration_ms=1,
    )
    commit_token = repository.issue_tool_commit_token(
        run_id=created.run["id"], plan_version=plan.version, step_id=step.id,
        lease_token=created.lease_token,
    )
    with pytest.raises(Exception, match="does not match"):
        runner.commit_step(
            created.run["id"], lease_token=created.lease_token, step_id=step.id,
            kind=step.kind, step_input=step.input,
            step_output={"status": "ok", "data": {"forged": True}, "evidence": []},
            idempotency_key=f"plan:{plan.version}:step:{step.id}", frontier=frontier,
            progress=20, budget_delta=decision.estimated_cost,
            tool={"name": step.tool_name, "version": "1", "input": step.input,
                  "output": {"status": "ok", "data": {"forged": True}, "evidence": []},
                  "duration_ms": 1, "cost_units": decision.estimated_cost,
                  "idempotency_key": f"tool:{plan.version}:{step.id}"},
            tool_commit_token=commit_token,
        )
    assert repository.get_runtime_snapshot(created.run["id"])["counts"]["steps"] == 0
