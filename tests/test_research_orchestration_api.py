import time

from fastapi.testclient import TestClient

from backend.app import create_app


def route(client, message, request_id, case_id=None):
    response = client.post(
        "/api/conversations/route",
        json={"message": message, "request_id": request_id, "case_id": case_id},
    )
    assert response.status_code == 200
    return response.json()


def wait_for_execution_checkpoint(client, run_id):
    for _ in range(200):
        task = client.get(f"/api/research/{run_id}").json()
        if task["status"] in {"failed", "completed"}:
            return task
        events = client.app.state.repository.list_events(run_id)
        if any(item["kind"] == "research.execution_completed" for item in events):
            return task
        time.sleep(0.01)
    raise AssertionError("Phase 3 execution did not reach its checkpoint")


def test_exact_entity_route_executes_through_verified_report_completion(tmp_path):
    app = create_app(tmp_path / "exact-api.db", mock_delay=0)
    with TestClient(app) as client:
        route(client, "研究腾讯盈利质量", "route-exact")
        response = client.post(
            "/api/research/intakes",
            json={"route_request_id": "route-exact", "depth": "quick", "budget_limit": 20},
        )
        assert response.status_code == 200
        intake = response.json()["intake"]
        assert intake["status"] == "running"
        assert intake["run_id"]
        task = wait_for_execution_checkpoint(client, intake["run_id"])
        assert task["status"] == "completed"
        assert task["progress"] == 100
        assert task["result"]["report_id"]
        assert task["result"]["degraded"] is True
        snapshot = app.state.repository.get_runtime_snapshot(intake["run_id"])
        assert snapshot["plan"]["version"] == 1
        assert snapshot["counts"]["tool_calls"] == 4
        assert all(item["status"] == "succeeded" for item in snapshot["tool_calls"])
        events = app.state.repository.list_events(intake["run_id"])
        assert sum(item["kind"] == "report.delta" for item in events) == 2
        assert [item["kind"] for item in events[-2:]] == ["report.completed", "run.completed"]


def test_ambiguous_entity_requires_confirmation_before_run_creation(tmp_path):
    app = create_app(tmp_path / "ambiguous-api.db", mock_delay=0)
    with TestClient(app) as client:
        route(client, "研究比亚迪盈利质量", "route-byd")
        response = client.post(
            "/api/research/intakes",
            json={"route_request_id": "route-byd", "depth": "quick", "budget_limit": 20},
        )
        intake = response.json()["intake"]
        assert intake["status"] == "awaiting_confirmation"
        assert intake["run_id"] is None
        assert len(intake["candidates"]) == 2
        assert client.get("/api/cases").json() == []

        confirm = client.post(
            f"/api/research/intakes/{intake['id']}/confirm",
            json={"candidate_id": intake["candidates"][0]["candidate_id"]},
        )
        assert confirm.status_code == 200
        linked = confirm.json()
        assert linked["status"] == "running"
        assert linked["run_id"]
        wait_for_execution_checkpoint(client, linked["run_id"])


def test_intake_start_is_idempotent_and_changed_budget_conflicts(tmp_path):
    app = create_app(tmp_path / "idempotent-api.db", mock_delay=0)
    with TestClient(app) as client:
        route(client, "研究腾讯盈利质量", "route-idempotent")
        payload = {"route_request_id": "route-idempotent", "depth": "quick", "budget_limit": 20}
        first = client.post("/api/research/intakes", json=payload)
        second = client.post("/api/research/intakes", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json()["intake"]["id"] == second.json()["intake"]["id"]
        assert first.json()["intake"]["run_id"] == second.json()["intake"]["run_id"]
        conflict = client.post(
            "/api/research/intakes", json={**payload, "budget_limit": 30}
        )
        assert conflict.status_code == 409


def test_social_route_is_forbidden_from_research_intake(tmp_path):
    app = create_app(tmp_path / "social-intake.db", mock_delay=0)
    with TestClient(app) as client:
        route(client, "谢谢", "route-social")
        response = client.post(
            "/api/research/intakes", json={"route_request_id": "route-social"}
        )
        assert response.status_code == 403
        assert client.get("/api/cases").json() == []


def test_followup_reuses_existing_case_but_creates_new_run(tmp_path):
    app = create_app(tmp_path / "followup-api.db", mock_delay=0)
    with TestClient(app) as client:
        legacy = client.post(
            "/api/research",
            json={"company": "腾讯控股", "symbol": "0700.HK", "market": "HK", "question": "分析腾讯盈利质量"},
        ).json()
        wait_for_execution_checkpoint(client, legacy["id"]) if False else None
        route(client, "再研究一下该公司的现金流", "route-followup", legacy["case_id"])
        response = client.post(
            "/api/research/intakes",
            json={"route_request_id": "route-followup", "depth": "quick", "budget_limit": 20},
        )
        intake = response.json()["intake"]
        assert intake["run_id"] != legacy["id"]
        new_task = client.get(f"/api/research/{intake['run_id']}").json()
        assert new_task["case_id"] == legacy["case_id"]
        assert len(client.get("/api/cases").json()) == 1


def test_explicit_different_company_creates_a_new_case(tmp_path):
    app = create_app(tmp_path / "case-split.db", mock_delay=0)
    with TestClient(app) as client:
        legacy = client.post(
            "/api/research",
            json={"company": "腾讯控股", "symbol": "0700.HK", "market": "HK", "question": "分析腾讯盈利质量"},
        ).json()
        route(client, "研究贵州茅台盈利质量", "route-maotai", legacy["case_id"])
        intake = client.post(
            "/api/research/intakes",
            json={"route_request_id": "route-maotai", "depth": "quick", "budget_limit": 20},
        ).json()["intake"]
        new_task = client.get(f"/api/research/{intake['run_id']}").json()
        assert new_task["case_id"] != legacy["case_id"]
        assert new_task["company"] == "贵州茅台"


def test_phase3_run_resumes_and_completes_phase4_after_restart(tmp_path):
    path = tmp_path / "restart-phase3.db"
    first_app = create_app(path, mock_delay=0.05)
    with TestClient(first_app) as client:
        route(client, "研究腾讯盈利质量", "route-restart")
        intake = client.post(
            "/api/research/intakes",
            json={"route_request_id": "route-restart", "depth": "deep", "budget_limit": 30},
        ).json()["intake"]
        run_id = intake["run_id"]

    second_app = create_app(path, mock_delay=0)
    with TestClient(second_app) as client:
        task = wait_for_execution_checkpoint(client, run_id)
        assert task["status"] == "completed"
        snapshot = second_app.state.repository.get_runtime_snapshot(run_id)
        assert snapshot["plan"]["version"] == 1
        assert all(call["tool_name"] != "provider_research" for call in snapshot["tool_calls"])
