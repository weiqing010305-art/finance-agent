from __future__ import annotations

import hashlib
from uuid import uuid4

from backend.database import Repository
from backend.schemas import AuthorizationDecision, PlanStep, RouteDecision
from backend.tool_registry import ToolRegistry


class PolicyGate:
    def __init__(self, repository: Repository, registry: ToolRegistry):
        self.repository = repository
        self.registry = registry

    def authorize(
        self,
        *,
        route: RouteDecision,
        run: dict,
        entity_confirmed: bool,
        plan_version: int,
        step: PlanStep,
        budget_limit: int,
        user_confirmed_high_risk: bool = False,
        lease_token: str | None = None,
        reserve: bool = True,
        principal=None,
    ) -> AuthorizationDecision:
        reasons: list[str] = []
        spec = self.registry.get(step.tool_name)
        if route.intent not in {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"} or not route.requires_planner:
            reasons.append("ROUTE_NOT_RESEARCH")
        if route.external_research_allowed:
            reasons.append("ROUTER_PERMISSION_FORBIDDEN")
        if not entity_confirmed:
            reasons.append("ENTITY_NOT_CONFIRMED")
        if spec is None:
            reasons.append("TOOL_NOT_REGISTERED")
        elif (spec.risk_level == "high" or spec.requires_confirmation) and not user_confirmed_high_risk:
            reasons.append("TOOL_CONFIRMATION_REQUIRED")
        effective_cost = max(step.estimated_cost, spec.cost_class if spec else 0)
        if spec is not None and step.estimated_cost < spec.cost_class:
            reasons.append("ESTIMATED_COST_BELOW_TOOL_FLOOR")
        remaining = max(0, int(budget_limit) - int(run["budget_used"]))
        if effective_cost > remaining:
            reasons.append("BUDGET_EXCEEDED")
        allowed = not reasons
        capability_token = None
        if allowed:
            capability_token = (
                hashlib.sha256(
                    f"{lease_token}:{run['id']}:{plan_version}:{step.id}:{step.tool_name}".encode("utf-8")
                ).hexdigest()
                if lease_token else str(uuid4())
            )
        decision = AuthorizationDecision(
            allowed=allowed, run_id=run["id"], plan_version=plan_version,
            step_id=step.id, tool_name=step.tool_name,
            estimated_cost=effective_cost, budget_before=remaining,
            reason_codes=["POLICY_ALLOW"] if allowed else reasons,
            capability_token=capability_token,
        )
        if reserve:
            self.repository.record_execution_authorization(
                run_id=run["id"], plan_version=plan_version, step_id=step.id,
                tool_name=step.tool_name, allowed=allowed,
                reason_codes=decision.reason_codes, estimated_cost=effective_cost,
                budget_before=remaining,
                capability_token=capability_token, effective_cost=effective_cost,
                budget_limit=budget_limit, principal=principal,
            )
        return decision
