from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from backend.database import Repository
from backend.entity_resolver import EntityResolver
from backend.schemas import ResearchIntakeStartRequest, RouteDecision


class ResearchIntakeState(TypedDict, total=False):
    route_request_id: str
    depth: str
    budget_limit: int
    route: dict[str, Any]
    current_case: dict[str, Any] | None
    resolution: dict[str, Any]
    intake: dict[str, Any]
    trace: list[str]


@dataclass(frozen=True)
class ResearchGraphResult:
    intake: dict[str, Any]
    trace: list[str]


class ResearchIntakeGraph:
    def __init__(self, repository: Repository, resolver: EntityResolver):
        self.repository = repository
        self.resolver = resolver
        graph = StateGraph(ResearchIntakeState)
        graph.add_node("load_route", self._load_route)
        graph.add_node("resolve_entity", self._resolve_entity)
        graph.add_node("persist_intake", self._persist_intake)
        graph.add_edge(START, "load_route")
        graph.add_edge("load_route", "resolve_entity")
        graph.add_edge("resolve_entity", "persist_intake")
        graph.add_edge("persist_intake", END)
        self.compiled = graph.compile()

    @staticmethod
    def _trace(state: ResearchIntakeState, node: str) -> list[str]:
        return [*state.get("trace", []), node]

    def _load_route(self, state: ResearchIntakeState) -> dict[str, Any]:
        route = self.repository.get_route_request(state["route_request_id"])
        if route is None:
            raise KeyError(state["route_request_id"])
        decision = RouteDecision.model_validate(route["decision"])
        if decision.intent not in {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"}:
            raise PermissionError("route is not a research request")
        current_case = self.repository.get_case(route["case_id"]) if route["case_id"] else None
        return {
            "route": route,
            "current_case": current_case,
            "trace": self._trace(state, "load_route"),
        }

    def _resolve_entity(self, state: ResearchIntakeState) -> dict[str, Any]:
        current = state.get("current_case")
        resolution = self.resolver.resolve(
            state["route"]["message"],
            current_company=current["company"] if current else None,
            current_symbol=current["symbol"] if current else None,
            current_market=current["market"] if current else None,
        )
        return {
            "resolution": resolution.model_dump(),
            "trace": self._trace(state, "resolve_entity"),
        }

    def _persist_intake(self, state: ResearchIntakeState) -> dict[str, Any]:
        ambiguous = state["resolution"]["status"] == "ambiguous"
        intake = self.repository.create_research_intake(
            state["route_request_id"],
            depth=state["depth"],
            budget_limit=int(state["budget_limit"]),
            resolution=state["resolution"],
            confirmation_id=str(uuid4()) if ambiguous else None,
            confirmation_expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat() if ambiguous else None,
        )
        return {"intake": intake, "trace": self._trace(state, "persist_intake")}

    def start(self, request: ResearchIntakeStartRequest) -> ResearchGraphResult:
        state = self.compiled.invoke({
            "route_request_id": request.route_request_id,
            "depth": request.depth,
            "budget_limit": request.budget_limit,
            "trace": [],
        })
        return ResearchGraphResult(intake=state["intake"], trace=state["trace"])
