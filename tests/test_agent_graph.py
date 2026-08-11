from __future__ import annotations

from dataclasses import asdict
import json

from backend.agent_graph import RoutingGraph
from backend.context_builder import CaseContext


class StubContextBuilder:
    def __init__(self, context: CaseContext):
        self.context = context
        self.calls = 0

    def build(self, case_id):
        self.calls += 1
        assert case_id == self.context.case_id
        return self.context


def test_graph_skips_context_node_for_high_confidence_rule_route():
    builder = StubContextBuilder(CaseContext())
    result = RoutingGraph(builder).route("好的，谢谢")

    assert result["decision"]["intent"] == "SOCIAL_ACK"
    assert result["trace"] == ["load_context", "rule_route", "finalize"]
    assert builder.calls == 1


def test_graph_uses_context_node_for_elliptical_financial_followup():
    context = CaseContext(
        case_id="case-1",
        active_run={"id": "run-1", "status": "running", "current_step": "reading", "progress": 40},
    )
    result = RoutingGraph(StubContextBuilder(context)).route("现金流", case_id="case-1")

    assert result["decision"]["intent"] == "RESEARCH_FOLLOWUP"
    assert result["decision"]["requires_planner"] is True
    assert result["decision"]["external_research_allowed"] is False
    assert result["trace"] == ["load_context", "rule_route", "context_route", "finalize"]


def test_graph_state_is_json_serializable_and_deterministic():
    graph = RoutingGraph(StubContextBuilder(CaseContext()))
    first = graph.route("研究腾讯")
    second = graph.route("研究腾讯")

    assert first["decision"] == second["decision"]
    json.dumps(first, ensure_ascii=False)
    assert graph.compiled.checkpointer is None


def test_graph_failure_falls_back_to_safe_clarification():
    class BrokenBuilder:
        def build(self, _case_id):
            raise RuntimeError("database unavailable")

    result = RoutingGraph(BrokenBuilder()).route("研究腾讯", case_id="case-1")

    assert result["decision"]["intent"] == "CLARIFICATION"
    assert result["decision"]["external_research_allowed"] is False
    assert result["decision"]["reason_codes"] == ["ROUTING_FAILURE"]
    assert "database unavailable" not in result["response"]


def test_graph_uses_langchain_human_message_boundary():
    result = RoutingGraph(StubContextBuilder(CaseContext())).route("暂停")
    assert result["message"] == "暂停"
    assert result["decision"]["intent"] == "CONTROL"
