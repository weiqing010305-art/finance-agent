import time

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


def wait_for_terminal(client: TestClient, task_id: str) -> dict:
    for _ in range(100):
        task = client.get(f"/api/research/{task_id}").json()
        if task["status"] in {"completed", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("mock research did not finish")


def test_health_and_financial_research_flow(tmp_path):
    app = create_app(tmp_path / "test.db", mock_delay=0.005)
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok", "mode": "mock"}

        response = client.post(
            "/api/research",
            json={
                "company": "腾讯控股",
                "symbol": "0700.HK",
                "market": "HK",
                "question": "腾讯近三年利润增长由哪些业务驱动？",
                "agent": "financial",
                "depth": "standard",
            },
        )
        assert response.status_code == 202
        task_id = response.json()["id"]

        task = wait_for_terminal(client, task_id)
        assert task["status"] == "completed"
        assert task["progress"] == 100
        assert task["result"]["synthetic"] is True
        assert [item["citation_number"] for item in task["evidence"]] == [1, 2, 3]

        cases = client.get("/api/cases").json()
        assert cases[0]["latest_task_id"] == task_id
        assert cases[0]["latest_status"] == "completed"


def test_only_financial_agent_is_enabled(tmp_path):
    app = create_app(tmp_path / "test.db", mock_delay=0)
    with TestClient(app) as client:
        response = client.post(
            "/api/research",
            json={
                "company": "腾讯控股",
                "question": "研究腾讯的市场表现",
                "agent": "market",
            },
        )
        assert response.status_code == 409
        assert "财报分析 Agent" in response.json()["detail"]


def test_file_frontend_origin_can_reach_api(tmp_path):
    app = create_app(tmp_path / "test.db", mock_delay=0)
    with TestClient(app) as client:
        response = client.options(
            "/api/research",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "null"


def test_removed_openrouter_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="must be 'mock' or 'deepseek'"):
        create_app(
            tmp_path / "test.db",
            research_mode="openrouter",
            load_env_file=False,
        )


def test_deepseek_mode_reports_model_and_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = create_app(
        tmp_path / "deepseek.db",
        research_mode="deepseek",
        load_env_file=False,
    )
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health == {
            "status": "ok",
            "mode": "deepseek",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "configured": "false",
        }
        response = client.post(
            "/api/research",
            json={"company": "自动识别中", "market": "AUTO", "question": "分析中国银行"},
        )
        assert response.status_code == 503
        assert "DEEPSEEK_API_KEY" in response.json()["detail"]


def test_create_research_idempotency_key_does_not_duplicate_run_or_worker(tmp_path):
    app = create_app(tmp_path / "idempotent.db", mock_delay=0.02)
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "same-create"}
        payload = {"company": "腾讯控股", "question": "分析利润增长是否可持续"}
        first = client.post("/api/research", json=payload, headers=headers)
        second = client.post("/api/research", json=payload, headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]

        task = wait_for_terminal(client, first.json()["id"])
        assert task["status"] == "completed"
        events = app.state.repository.list_events(task["id"])
        assert [event["kind"] for event in events].count("run.started") == 1
        assert [event["kind"] for event in events].count("step.completed") == 5

        completed_retry = client.post("/api/research", json=payload, headers=headers)
        assert completed_retry.status_code == 202
        assert completed_retry.json()["id"] == task["id"]

        conflict = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "改成另一个研究问题"},
            headers=headers,
        )
        assert conflict.status_code == 409


def test_pause_resume_feedback_and_legacy_cancel_maps_to_safe_pause(tmp_path):
    app = create_app(tmp_path / "test.db", mock_delay=0.08)
    with TestClient(app) as client:
        task = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        ).json()
        task_id = task["id"]

        paused = client.post(f"/api/research/{task_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "pause_requested"

        for _ in range(100):
            current = client.get(f"/api/research/{task_id}").json()
            if current["status"] == "paused":
                break
            time.sleep(0.01)
        assert current["status"] == "paused"

        feedback = client.post(
            f"/api/research/{task_id}/feedback",
            json={"message": "重点补查现金流"},
        )
        assert feedback.status_code == 200

        resumed = client.post(f"/api/research/{task_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "running"

        stopped = client.post(f"/api/research/{task_id}/cancel")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "pause_requested"


def test_sse_last_event_id_replays_only_later_persisted_events(tmp_path):
    app = create_app(tmp_path / "sse.db", mock_delay=0)
    with TestClient(app) as client:
        created = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        ).json()
        wait_for_terminal(client, created["id"])
        events = app.state.repository.list_events(created["id"])
        cursor = events[2]["id"]
        expected_ids = [event["id"] for event in events if event["id"] > cursor]

        with client.stream(
            "GET",
            f"/api/research/{created['id']}/events",
            headers={"Last-Event-ID": str(cursor)},
        ) as response:
            replayed_ids = [
                int(line.removeprefix("id: "))
                for line in response.iter_lines()
                if line.startswith("id: ")
            ]

        assert replayed_ids == expected_ids


def test_sse_rejects_non_numeric_last_event_id(tmp_path):
    app = create_app(tmp_path / "bad-sse.db", mock_delay=0)
    with TestClient(app) as client:
        task = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        ).json()
        response = client.get(
            f"/api/research/{task['id']}/events",
            headers={"Last-Event-ID": "not-a-number"},
        )
        assert response.status_code == 400


def test_paused_create_retry_is_idempotent(tmp_path):
    app = create_app(tmp_path / "paused-idempotent.db", mock_delay=0.1)
    headers = {"Idempotency-Key": "paused-key"}
    payload = {"company": "腾讯控股", "question": "分析利润增长是否可持续"}
    with TestClient(app) as client:
        first = client.post("/api/research", json=payload, headers=headers).json()
        client.post(f"/api/research/{first['id']}/pause")
        for _ in range(100):
            paused = client.get(f"/api/research/{first['id']}").json()
            if paused["status"] == "paused":
                break
            time.sleep(0.01)
        retry = client.post("/api/research", json=payload, headers=headers)
        assert retry.status_code == 202
        assert retry.json()["id"] == first["id"]
        assert retry.json()["status"] == "paused"


def test_evidence_enrichment_rejects_active_run_before_external_call(tmp_path, monkeypatch):
    called = False

    async def should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(
        "backend.deepseek_research.DeepSeekResearchClient.enrich_evidence",
        should_not_run,
    )
    app = create_app(tmp_path / "enrich-active.db", mock_delay=0.1)
    with TestClient(app) as client:
        task = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        ).json()
        response = client.post(f"/api/research/{task['id']}/evidence/enrich")
        assert response.status_code == 409
        assert called is False
