import pytest

from backend.database import Repository
from backend.entity_resolver import EntityResolver
from backend.research_graph import ResearchIntakeGraph
from backend.schemas import ResearchIntakeStartRequest


def save_route(repository, request_id, message, *, intent="RESEARCH_NEW", case_id=None):
    repository.save_route_request_result(
        request_id, case_id=case_id, message=message,
        decision={
            "intent": intent, "confidence": 1, "case_id": case_id,
            "requires_planner": intent in {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"},
            "external_research_allowed": False,
            "response_policy": "await_entity_resolution", "reason_codes": [],
        },
        response="ok", trace=[],
    )


def test_intake_graph_has_explicit_nodes_and_persists_resolution(tmp_path):
    repository = Repository(tmp_path / "graph.db")
    repository.initialize()
    save_route(repository, "route-tencent", "研究腾讯")
    graph = ResearchIntakeGraph(repository, EntityResolver())
    result = graph.start(ResearchIntakeStartRequest(route_request_id="route-tencent"))

    assert result.trace == ["load_route", "resolve_entity", "persist_intake"]
    assert result.intake["status"] == "ready"
    assert result.intake["resolved_entity"]["symbol"] == "0700.HK"


def test_intake_graph_rejects_non_research_route(tmp_path):
    repository = Repository(tmp_path / "denied-graph.db")
    repository.initialize()
    save_route(repository, "route-social", "谢谢", intent="SOCIAL_ACK")
    with pytest.raises(PermissionError):
        ResearchIntakeGraph(repository, EntityResolver()).start(
            ResearchIntakeStartRequest(route_request_id="route-social")
        )
