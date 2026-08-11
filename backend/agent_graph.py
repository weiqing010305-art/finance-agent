from __future__ import annotations

from dataclasses import asdict
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from backend.context_builder import CaseContext, ContextBuilder
from backend.intent_router import RoutingContext, route_by_rules
from backend.schemas import RouteDecision


class RoutingState(TypedDict, total=False):
    message: str
    case_id: str | None
    context: dict[str, Any]
    rule_decision: dict[str, Any]
    decision: dict[str, Any]
    response: str
    trace: list[str]


_FINANCIAL_FOLLOWUP_TERMS = {
    "现金流", "利润", "盈利", "收入", "营收", "毛利", "负债", "估值", "风险", "分红",
}


def _response_for(decision: RouteDecision) -> str:
    responses = {
        "SOCIAL_ACK": "不客气。如果还想继续研究这家公司，直接告诉我关注点即可。",
        "CONTROL": "已识别为任务控制请求，将由运行状态接口处理。",
        "CONFIRMATION": "已识别为确认回复，等待确认处理器校验并应用后再继续。",
        "REPORT_QA": "这个问题应优先基于现有报告和证据回答，不会自动联网搜索。",
        "RESEARCH_FOLLOWUP": "已识别为当前研究的补充问题，等待进入 Planner。",
        "RESEARCH_NEW": "已识别为新的研究请求，下一步需要解析并确认公司实体。",
        "CLARIFICATION": "请补充你想研究的公司或具体问题。",
        "OUT_OF_SCOPE": "当前 Agent 只处理公司与金融研究相关问题。",
        "AMBIGUOUS": "我还不能确定你是想继续研究、询问报告，还是控制当前任务，请再说明一下。",
    }
    return responses[decision.intent]


class RoutingGraph:
    def __init__(self, context_builder: ContextBuilder):
        self.context_builder = context_builder
        graph = StateGraph(RoutingState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("rule_route", self._rule_route)
        graph.add_node("context_route", self._context_route)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "rule_route")
        graph.add_conditional_edges(
            "rule_route",
            self._after_rule,
            {"context_route": "context_route", "finalize": "finalize"},
        )
        graph.add_edge("context_route", "finalize")
        graph.add_edge("finalize", END)
        self.compiled = graph.compile()

    @staticmethod
    def _trace(state: RoutingState, node: str) -> list[str]:
        return [*state.get("trace", []), node]

    def _load_context(self, state: RoutingState) -> dict[str, Any]:
        context = self.context_builder.build(state.get("case_id"))
        return {"context": asdict(context), "trace": self._trace(state, "load_context")}

    def _rule_route(self, state: RoutingState) -> dict[str, Any]:
        context = state["context"]
        active_run = context.get("active_run")
        routing_context = RoutingContext(
            case_id=context.get("case_id"),
            company=context.get("company"),
            has_pending_confirmation=context.get("pending_confirmation") is not None,
            active_run_status=active_run.get("status") if active_run else None,
            has_report=bool(context.get("has_report")),
            report_has_evidence=bool(context.get("report_has_evidence")),
        )
        decision = route_by_rules(state["message"], routing_context)
        return {
            "rule_decision": decision.model_dump(),
            "decision": decision.model_dump(),
            "trace": self._trace(state, "rule_route"),
        }

    @staticmethod
    def _after_rule(state: RoutingState) -> str:
        return "context_route" if state["decision"]["intent"] == "AMBIGUOUS" else "finalize"

    def _context_route(self, state: RoutingState) -> dict[str, Any]:
        context = state["context"]
        message = state["message"]
        decision = RouteDecision.model_validate(state["decision"])
        if context.get("case_id") and any(term in message for term in _FINANCIAL_FOLLOWUP_TERMS):
            if context.get("has_report") and context.get("report_has_evidence"):
                decision = RouteDecision(
                    intent="REPORT_QA", confidence=0.84, case_id=context["case_id"],
                    requires_planner=False, external_research_allowed=False,
                    response_policy="answer_from_existing_evidence",
                    reason_codes=["ACTIVE_CASE", "FINANCIAL_ELLIPSIS", "EXISTING_EVIDENCE"],
                )
            elif context.get("active_run") or context.get("has_report"):
                decision = RouteDecision(
                    intent="RESEARCH_FOLLOWUP", confidence=0.82, case_id=context["case_id"],
                    requires_planner=True, external_research_allowed=False,
                    response_policy="enqueue_at_safe_checkpoint",
                    reason_codes=["ACTIVE_CASE", "FINANCIAL_ELLIPSIS"],
                )
        return {
            "decision": decision.model_dump(),
            "trace": self._trace(state, "context_route"),
        }

    def _finalize(self, state: RoutingState) -> dict[str, Any]:
        decision = RouteDecision.model_validate(state["decision"])
        return {
            "response": _response_for(decision),
            "trace": self._trace(state, "finalize"),
        }

    def route(self, message: str, *, case_id: str | None = None) -> dict[str, Any]:
        human_message = HumanMessage(content=message)
        normalized = human_message.content if isinstance(human_message.content, str) else str(human_message.content)
        try:
            state = self.compiled.invoke({
                "message": normalized,
                "case_id": case_id,
                "trace": [],
            })
            return {
                "message": normalized,
                "case_id": case_id,
                "decision": state["decision"],
                "response": state["response"],
                "trace": state["trace"],
            }
        except Exception:
            decision = RouteDecision(
                intent="CLARIFICATION", confidence=0.0, case_id=case_id,
                requires_planner=False, external_research_allowed=False,
                response_policy="ask_clarification", reason_codes=["ROUTING_FAILURE"],
            )
            return {
                "message": normalized,
                "case_id": case_id,
                "decision": decision.model_dump(),
                "response": _response_for(decision),
                "trace": ["routing_failure"],
            }
