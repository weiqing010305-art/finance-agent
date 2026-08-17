from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from backend.durable_runner import DurableRunner, RunConflict
from backend.planner import DeterministicPlanner, PlannerError
from backend.policy import PolicyGate
from backend.schemas import PlanStep, ResearchPlan, RouteDecision
from backend.tool_registry import (
    ToolExecution, ToolInvocationContext, ToolRegistry, ToolRegistryError,
)


class ExecutionDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class BatchResult:
    executed_step_ids: list[str]
    reused_step_ids: list[str]
    status: str
    replanned: bool = False


class ResearchExecutor:
    def __init__(
        self,
        runner: DurableRunner,
        registry: ToolRegistry,
        policy: PolicyGate,
        planner: DeterministicPlanner | None = None,
    ):
        self.runner = runner
        self.registry = registry
        self.policy = policy
        self.planner = planner or DeterministicPlanner()

    @staticmethod
    def _ready(plan: ResearchPlan, completed: set[str]) -> list[PlanStep]:
        return [
            step for step in plan.steps
            if step.id not in completed and set(step.dependencies) <= completed
        ]

    @staticmethod
    def _frontier(plan: ResearchPlan, completed: set[str]) -> dict[str, Any]:
        ready = ResearchExecutor._ready(plan, completed)
        ready_ids = {step.id for step in ready}
        blocked = [
            step.id for step in plan.steps
            if step.id not in completed and step.id not in ready_ids
        ]
        return {
            "plan_version": plan.version,
            "ready_step_ids": [step.id for step in ready],
            "running_step_ids": [],
            "blocked_step_ids": blocked,
            "completed_step_ids": sorted(completed),
        }

    @staticmethod
    def _completed_outputs(
        snapshot: dict[str, Any], plan_version: int,
    ) -> dict[str, dict[str, Any]]:
        """Succeeded step outputs of the CURRENT plan version, keyed by step id.

        Steps carry their step id inside the row id as ``{run_id}:{step_id}``
        (see durable_runner.commit_step); only succeeded steps of the active
        plan version feed downstream tool inputs, so a replan can never leak
        stale observations from an older plan into the current one.
        """
        run_id = str(snapshot["run"]["id"])
        outputs: dict[str, dict[str, Any]] = {}
        for row in snapshot["steps"]:
            if int(row.get("plan_version") or 0) != plan_version:
                continue
            if row.get("status") != "succeeded" or not row.get("output_json"):
                continue
            step_id = str(row["id"]).removeprefix(f"{run_id}:")
            try:
                output = json.loads(row["output_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(output, dict):
                outputs[step_id] = output
        return outputs

    @staticmethod
    def _collect_document_texts(
        outputs: dict[str, dict[str, Any]],
        *,
        explicit: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Gather document/excerpt texts from succeeded tool outputs.

        The explicit list (already supplied in the step input) wins; otherwise
        every succeeded step's ``data`` items that carry extractable text
        (document sections, retrieval chunks, search snippets) are collected
        into ``{source_id, text}`` pairs bounded to the extractor contract.
        """
        if explicit:
            return explicit
        texts: list[dict[str, Any]] = []
        for output in outputs.values():
            data = output.get("data")
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") or item.get("snippet") or item.get("excerpt") or ""
                if not isinstance(text, str) or not text.strip():
                    continue
                source_id = (
                    item.get("source_id")
                    or item.get("url")
                    or item.get("document_id")
                    or item.get("chunk_id")
                    or ""
                )
                if not source_id:
                    continue
                texts.append({
                    "source_id": str(source_id)[:200],
                    "text": text[:50_000],
                })
                if len(texts) >= 100:
                    return texts
        return texts

    @staticmethod
    def _collect_extracted_facts(
        outputs: dict[str, dict[str, Any]],
        *,
        explicit: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Gather extracted financial facts for the metrics calculator.

        A fact item is recognised structurally (name + period + value, without
        a document ``text`` field), which excludes document sections, search
        hits and already-computed metrics from being re-fed.
        """
        if explicit:
            return explicit
        facts: list[dict[str, Any]] = []
        for output in outputs.values():
            data = output.get("data")
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict) or item.get("text") is not None:
                    continue
                name = item.get("name")
                period = item.get("period")
                value = item.get("value")
                if not name or not period or value is None:
                    continue
                facts.append({
                    "name": str(name)[:100],
                    "value": value,
                    "period": str(period)[:40],
                    "unit": str(item.get("unit") or "")[:20],
                    "currency": str(item["currency"])[:16] if item.get("currency") else None,
                })
                if len(facts) >= 500:
                    return facts
        return facts

    def _enrich_step_input(
        self,
        step: PlanStep,
        outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Compose the effective tool input for one ready step.

        Downstream tools receive the observations of already-succeeded steps:
        ``extract_financial_facts`` gets document texts, and
        ``calculate_financial_metrics`` gets the extracted facts. Explicit
        values already present in the planner's payload are never overwritten.
        """
        base = dict(step.input)
        if step.tool_name == "extract_financial_facts":
            base["texts"] = self._collect_document_texts(outputs, explicit=base.get("texts"))
        elif step.tool_name == "calculate_financial_metrics":
            base["facts"] = self._collect_extracted_facts(outputs, explicit=base.get("facts"))
        return base

    async def execute_ready_batch(
        self,
        run_id: str,
        *,
        lease_token: str,
        route: RouteDecision,
        entity_confirmed: bool,
        budget_limit: int,
    ) -> BatchResult:
        snapshot = self.runner.repository.get_runtime_snapshot(run_id)
        run = snapshot["run"]
        if run["status"] != "running":
            return BatchResult([], [], run["status"])
        plan = ResearchPlan.model_validate(snapshot["plan"])
        completed = set(snapshot["checkpoint"]["frontier"].get("completed_step_ids", []))
        ready = self._ready(plan, completed)
        if not ready:
            return BatchResult([], [], run["status"])

        remaining_budget = int(budget_limit) - int(run["budget_used"])
        if sum(step.estimated_cost for step in ready) > remaining_budget:
            raise ExecutionDenied("ready frontier exceeds remaining budget")
        decisions = {}
        for step in ready:
            self.runner.renew_lease(run_id, lease_token=lease_token)
            existing_claim = self.runner.repository.get_tool_execution_claim(
                run_id, plan.version, step.id
            )
            already_observed = existing_claim is not None and existing_claim["status"] == "observed"
            decision = self.policy.authorize(
                route=route, run=run, entity_confirmed=entity_confirmed,
                plan_version=plan.version, step=step, budget_limit=budget_limit,
                lease_token=lease_token,
                reserve=not already_observed,
            )
            if already_observed:
                decision = decision.model_copy(update={"capability_token": None})
            if not decision.allowed:
                raise ExecutionDenied(
                    f"step {step.id} denied: {','.join(decision.reason_codes)}"
                )
            decisions[step.id] = decision

        reused: dict[str, dict[str, Any]] = {}
        calls: list[tuple[PlanStep, dict[str, Any], asyncio.Task[ToolExecution]]] = []
        completed_outputs = self._completed_outputs(snapshot, plan.version)
        enriched_inputs: dict[str, dict[str, Any]] = {
            step.id: self._enrich_step_input(step, completed_outputs)
            for step in ready
        }
        for step in ready:
            self.runner.renew_lease(run_id, lease_token=lease_token)
            idempotency_key = f"plan:{plan.version}:step:{step.id}"
            step_input = enriched_inputs[step.id]
            cached = self.runner.repository.get_completed_step_output(run_id, idempotency_key)
            if cached is not None:
                reused[step.id] = cached
            else:
                existing_claim = self.runner.repository.get_tool_execution_claim(
                    run_id, plan.version, step.id
                )
                if existing_claim is not None and existing_claim["status"] == "observed":
                    reused[step.id] = {"claim": existing_claim, "output": existing_claim["output"]}
                    continue
                claim = self.runner.repository.claim_tool_execution(
                    run_id=run_id, plan_version=plan.version, step_id=step.id,
                    tool_name=step.tool_name, lease_token=lease_token,
                    capability_token=decisions[step.id].capability_token or "",
                    idempotency_key=f"tool:{plan.version}:{step.id}", step_input=step.input,
                )
                if claim["status"] == "observed":
                    reused[step.id] = {"claim": claim, "output": claim["output"]}
                elif claim["execution_token"] is not None:
                    context = ToolInvocationContext(
                        run_id=run_id, plan_version=plan.version, step_id=step.id,
                        idempotency_key=claim["idempotency_key"],
                    )
                    calls.append((step, claim, asyncio.create_task(
                        self.registry.execute(step.tool_name, step_input, context=context)
                    )))

        async def heartbeat() -> None:
            interval = max(0.01, min(5.0, self.runner.lease_ttl.total_seconds() / 3))
            while True:
                await asyncio.sleep(interval)
                self.runner.renew_lease(run_id, lease_token=lease_token)

        heartbeat_task = asyncio.create_task(heartbeat()) if calls else None
        try:
            results = await asyncio.gather(
                *(task for _step, _claim, task in calls), return_exceptions=True
            )
            if calls:
                self.runner.renew_lease(run_id, lease_token=lease_token)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
        executions: dict[str, ToolExecution] = {}
        for (step, claim, _task), result in zip(calls, results, strict=True):
            if isinstance(result, BaseException):
                for _other_step, _other_claim, other_task in calls:
                    if not other_task.done():
                        other_task.cancel()
                if isinstance(result, ToolRegistryError):
                    raise result
                raise ToolRegistryError(f"tool failed: {step.tool_name}: {result}") from result
            sanitized = self.runner.repository.record_tool_observation(
                run_id=run_id, plan_version=plan.version, step_id=step.id,
                lease_token=lease_token, execution_token=claim["execution_token"],
                output=result.output, duration_ms=result.duration_ms,
            )
            executions[step.id] = ToolExecution(
                spec=result.spec, output=sanitized, duration_ms=result.duration_ms
            )

        for step in ready:
            if step.id in reused and "claim" in reused[step.id]:
                spec = self.registry.get(step.tool_name)
                claim = reused[step.id]["claim"]
                executions[step.id] = ToolExecution(
                    spec=spec, output=reused[step.id]["output"],
                    duration_ms=int(claim.get("duration_ms") or 0),
                )

        insufficient = [
            step_id for step_id, execution in executions.items()
            if execution.output.get("status") == "insufficient"
        ]
        executed_ids: list[str] = []
        total_steps = len(plan.steps)
        for step in ready:
            if step.id in reused and "claim" not in reused[step.id]:
                completed.add(step.id)
                continue
            if step.id not in executions:
                continue
            if executions[step.id].output.get("status") == "insufficient":
                continue
            latest = self.runner.repository.get_task(run_id)
            if latest is None or latest["status"] != "running":
                return BatchResult(executed_ids, list(reused), latest["status"] if latest else "missing")
            execution = executions[step.id]
            commit_token = self.runner.repository.issue_tool_commit_token(
                run_id=run_id, plan_version=plan.version, step_id=step.id,
                lease_token=lease_token,
            )
            completed.add(step.id)
            frontier = self._frontier(plan, completed)
            progress = min(95, 5 + int(90 * len(completed) / max(1, total_steps)))
            self.runner.commit_step(
                run_id,
                lease_token=lease_token,
                step_id=step.id,
                kind=step.kind,
                step_input=step.input,
                step_output=execution.output,
                idempotency_key=f"plan:{plan.version}:step:{step.id}",
                frontier=frontier,
                progress=progress,
                budget_delta=decisions[step.id].estimated_cost,
                tool={
                    "name": execution.spec.name,
                    "version": execution.spec.version,
                    "input": step.input,
                    "output": execution.output,
                    "duration_ms": execution.duration_ms,
                    "cost_units": decisions[step.id].estimated_cost,
                    "idempotency_key": f"tool:{plan.version}:{step.id}",
                },
                capability_token=decisions[step.id].capability_token,
                tool_commit_token=commit_token,
            )
            executed_ids.append(step.id)
        if insufficient:
            for step_id in insufficient:
                self.runner.repository.abandon_tool_claim(
                    run_id=run_id, plan_version=plan.version, step_id=step_id,
                    lease_token=lease_token,
                    reason="insufficient observation triggered replan",
                )
            if plan.fallback_used:
                raise ExecutionDenied(
                    f"insufficient observation after replan: {','.join(insufficient)}"
                )
            replanned = self.replan_once(
                run_id, lease_token=lease_token,
                reason=f"insufficient observation: {','.join(insufficient)}",
                budget_limit=budget_limit,
            )
            return BatchResult(executed_ids, list(reused), "running", replanned=True)
        latest = self.runner.repository.get_task(run_id)
        return BatchResult(executed_ids, list(reused), latest["status"])

    def replan_once(
        self,
        run_id: str,
        *,
        lease_token: str,
        reason: str,
        budget_limit: int,
    ) -> ResearchPlan:
        snapshot = self.runner.repository.get_runtime_snapshot(run_id)
        current = ResearchPlan.model_validate(snapshot["plan"])
        remaining = int(budget_limit) - int(snapshot["run"]["budget_used"])
        try:
            plan = self.planner.replan(current, reason=reason, remaining_budget=remaining)
            self.runner.install_plan(run_id, lease_token=lease_token, plan=plan.model_dump())
            return plan
        except (PlannerError, RunConflict) as exc:
            raise ExecutionDenied(str(exc)) from exc
