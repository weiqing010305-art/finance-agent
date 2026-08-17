from __future__ import annotations

from backend.schemas import PlanStep, ResearchPlan, SecurityCandidate


class PlannerError(ValueError):
    pass


class DeterministicPlanner:
    """Auditable fallback planner used until a model planner proves better in evals."""

    def create_plan(
        self,
        *,
        question: str,
        entity: SecurityCandidate,
        depth: str,
        budget_limit: int,
        version: int = 2,
    ) -> ResearchPlan:
        common = {
            "company": entity.company,
            "symbol": entity.symbol,
            "market": entity.market,
            "question": question,
        }
        steps = [
            PlanStep(
                id="search_filings",
                tool_name="search_filings",
                input={**common, "document_types": ["annual_report", "interim_report"]},
                success_criteria=["find at least one official filing"],
                max_attempts=2,
                estimated_cost=2,
            ),
            PlanStep(
                id="get_quote",
                tool_name="get_quote",
                input={"symbol": entity.symbol or "", "market": entity.market or ""},
                success_criteria=["fetch one deterministic quote for the confirmed security"],
                estimated_cost=1,
            ),
            PlanStep(
                id="search_web",
                tool_name="search_web",
                input={**common, "domains": [], "max_results": 8},
                success_criteria=["find at least two independent public sources"],
                max_attempts=2,
                estimated_cost=3,
            ),
            PlanStep(
                id="retrieve_documents",
                tool_name="retrieve_documents",
                input={
                    **common,
                    "retrieval_mode": "hybrid",
                    "fusion": "rrf",
                    "top_k": 10,
                },
                success_criteria=["retrieve traceable document chunks"],
                estimated_cost=2,
            ),
            PlanStep(
                id="extract_facts",
                tool_name="extract_financial_facts",
                dependencies=["search_filings", "retrieve_documents"],
                input={**common, "periods": 3},
                success_criteria=["extract facts with period unit currency and source"],
                estimated_cost=3,
            ),
            PlanStep(
                id="calculate_metrics",
                tool_name="calculate_financial_metrics",
                dependencies=["extract_facts"],
                input={"question": question, "metrics": ["growth", "margin", "cash_conversion"]},
                success_criteria=["calculate deterministic metrics from extracted facts"],
                estimated_cost=1,
            ),
        ]
        if depth == "quick":
            steps = [step for step in steps if step.id not in {"search_web"}]
        elif depth == "deep":
            steps.insert(
                3,
                PlanStep(
                    id="read_documents",
                    tool_name="read_document",
                    dependencies=["search_filings", "retrieve_documents"],
                    input={**common, "selection": "top_authoritative"},
                    success_criteria=["read primary document sections with page references"],
                    estimated_cost=3,
                ),
            )
            for index, step in enumerate(steps):
                if step.id == "extract_facts":
                    steps[index] = step.model_copy(
                        update={"dependencies": ["search_filings", "retrieve_documents", "read_documents"]}
                    )
        plan = ResearchPlan(version=version, goal=question, steps=steps)
        if plan.estimated_cost > budget_limit:
            raise PlannerError(
                f"estimated plan cost {plan.estimated_cost} exceeds budget {budget_limit}"
            )
        return plan

    def replan(
        self,
        plan: ResearchPlan,
        *,
        reason: str,
        remaining_budget: int,
    ) -> ResearchPlan:
        if plan.fallback_used or plan.max_replans < 1:
            raise PlannerError("automatic replan limit reached")
        extra = PlanStep(
            id="search_web_fallback",
            tool_name="search_web",
            input={"query": plan.goal, "reason": reason, "max_results": 5},
            success_criteria=["obtain a fallback source for insufficient evidence"],
            max_attempts=1,
            estimated_cost=2,
        )
        candidate = ResearchPlan(
            version=plan.version + 1,
            goal=plan.goal,
            steps=[*plan.steps, extra],
            max_replans=1,
            fallback_used=True,
        )
        if extra.estimated_cost > remaining_budget:
            raise PlannerError("replanned cost exceeds remaining budget")
        return candidate
