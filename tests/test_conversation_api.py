from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.app import create_app


def create_case(client: TestClient) -> dict:
    response = client.post(
        "/api/research",
        json={"company": "腾讯控股", "question": "分析腾讯盈利质量"},
    )
    assert response.status_code == 202
    return response.json()


def test_social_route_without_case_creates_no_run_or_tool_call(tmp_path):
    app = create_app(tmp_path / "social-route.db", mock_delay=0)
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations/route",
            json={"message": "好的，谢谢", "request_id": "social-1"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["decision"]["intent"] == "SOCIAL_ACK"
        assert body["decision"]["external_research_allowed"] is False
        assert client.get("/api/cases").json() == []


def test_known_case_route_persists_turns_without_creating_another_run(tmp_path):
    app = create_app(tmp_path / "case-route.db", mock_delay=0.2)
    with TestClient(app) as client:
        task = create_case(client)
        response = client.post(
            "/api/conversations/route",
            json={
                "message": "再看看现金流",
                "case_id": task["case_id"],
                "request_id": "followup-1",
            },
        )

        assert response.status_code == 200
        assert response.json()["decision"]["intent"] == "RESEARCH_FOLLOWUP"
        turns = app.state.repository.list_conversation_turns(task["case_id"])
        assert [turn["role"] for turn in turns] == ["user", "assistant"]
        assert turns[1]["intent"] == "RESEARCH_FOLLOWUP"
        cases = client.get("/api/cases").json()
        assert len(cases) == 1
        assert cases[0]["latest_task_id"] == task["id"]


def test_route_request_id_is_idempotent_and_conflicts_on_changed_message(tmp_path):
    app = create_app(tmp_path / "route-idempotency.db", mock_delay=0.2)
    with TestClient(app) as client:
        task = create_case(client)
        payload = {
            "message": "好的，谢谢",
            "case_id": task["case_id"],
            "request_id": "same-route",
        }
        first = client.post("/api/conversations/route", json=payload)
        second = client.post("/api/conversations/route", json=payload)

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert len(app.state.repository.list_conversation_turns(task["case_id"])) == 2

        conflict = client.post(
            "/api/conversations/route",
            json={**payload, "message": "研究现金流"},
        )
        assert conflict.status_code == 409


def test_unknown_case_is_rejected(tmp_path):
    app = create_app(tmp_path / "unknown-case.db", mock_delay=0)
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations/route",
            json={"message": "好的", "case_id": "missing", "request_id": "missing"},
        )
        assert response.status_code == 404


def test_control_route_does_not_bypass_durable_runner(tmp_path):
    app = create_app(tmp_path / "control-route.db", mock_delay=1)
    with TestClient(app) as client:
        task = create_case(client)
        response = client.post(
            "/api/conversations/route",
            json={"message": "暂停", "case_id": task["case_id"], "request_id": "control"},
        )
        assert response.status_code == 200
        assert response.json()["decision"]["intent"] == "CONTROL"
        assert client.get(f"/api/research/{task['id']}").json()["status"] == "running"


def test_unexpected_graph_error_returns_safe_clarification(tmp_path):
    app = create_app(tmp_path / "graph-error.db", mock_delay=0)

    class BrokenGraph:
        def route(self, *_args, **_kwargs):
            raise RuntimeError("secret internal failure")

    with TestClient(app) as client:
        app.state.routing_graph = BrokenGraph()
        response = client.post(
            "/api/conversations/route",
            json={"message": "研究腾讯", "request_id": "broken"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["decision"]["intent"] == "CLARIFICATION"
        assert body["decision"]["external_research_allowed"] is False
        assert "secret internal failure" not in body["response"]


def test_route_retry_replays_original_result_after_context_changes(tmp_path):
    app = create_app(tmp_path / "route-replay.db", mock_delay=0.05)
    with TestClient(app) as client:
        task = create_case(client)
        payload = {
            "message": "现金流",
            "case_id": task["case_id"],
            "request_id": "context-drift",
        }
        first = client.post("/api/conversations/route", json=payload)
        assert first.status_code == 200
        assert first.json()["decision"]["intent"] == "RESEARCH_FOLLOWUP"
        for _ in range(100):
            if client.get(f"/api/research/{task['id']}").json()["status"] == "completed":
                break
            time.sleep(0.01)

        replay = client.post("/api/conversations/route", json=payload)
        assert replay.status_code == 200
        assert replay.json() == first.json()


def test_route_request_id_is_global_even_without_case(tmp_path):
    app = create_app(tmp_path / "global-route-id.db", mock_delay=0)
    with TestClient(app) as client:
        first = client.post(
            "/api/conversations/route",
            json={"message": "好的", "request_id": "global-id"},
        )
        assert first.status_code == 200
        replay = client.post(
            "/api/conversations/route",
            json={"message": "好的", "request_id": "global-id"},
        )
        assert replay.json() == first.json()
        conflict = client.post(
            "/api/conversations/route",
            json={"message": "研究腾讯", "request_id": "global-id"},
        )
        assert conflict.status_code == 409


def test_blank_route_input_is_rejected_consistently(tmp_path):
    app = create_app(tmp_path / "blank-route.db", mock_delay=0)
    with TestClient(app) as client:
        task = create_case(client)
        without_case = client.post("/api/conversations/route", json={"message": "   "})
        with_case = client.post(
            "/api/conversations/route",
            json={"message": "   ", "case_id": task["case_id"]},
        )
        assert without_case.status_code == with_case.status_code == 422


def test_inconsistent_graph_permission_output_is_safely_downgraded(tmp_path):
    app = create_app(tmp_path / "invalid-decision.db", mock_delay=0)

    class InvalidPermissionGraph:
        def route(self, *_args, **_kwargs):
            return {
                "decision": {
                    "intent": "SOCIAL_ACK",
                    "confidence": 1.0,
                    "case_id": None,
                    "requires_planner": False,
                    "external_research_allowed": True,
                    "response_policy": "template_reply",
                    "reason_codes": [],
                },
                "response": "unsafe",
                "trace": [],
            }

    with TestClient(app) as client:
        app.state.routing_graph = InvalidPermissionGraph()
        response = client.post(
            "/api/conversations/route",
            json={"message": "好的", "request_id": "invalid-permission"},
        )
        assert response.status_code == 200
        assert response.json()["decision"]["intent"] == "CLARIFICATION"
        assert response.json()["decision"]["external_research_allowed"] is False
