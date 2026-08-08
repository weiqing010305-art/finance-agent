import time

from fastapi.testclient import TestClient

from backend.app import create_app


def wait_for_terminal(client: TestClient, task_id: str) -> dict:
    for _ in range(100):
        task = client.get(f"/api/research/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled"}:
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


def test_openrouter_mode_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = create_app(
        tmp_path / "test.db",
        research_mode="openrouter",
        load_env_file=False,
    )
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["mode"] == "openrouter"
        assert health["configured"] == "false"

        response = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        )
        assert response.status_code == 503
        assert "OPENROUTER_API_KEY" in response.json()["detail"]


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


def test_pause_resume_feedback_and_cancel(tmp_path):
    app = create_app(tmp_path / "test.db", mock_delay=0.08)
    with TestClient(app) as client:
        task = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        ).json()
        task_id = task["id"]

        paused = client.post(f"/api/research/{task_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"

        feedback = client.post(
            f"/api/research/{task_id}/feedback",
            json={"message": "重点补查现金流"},
        )
        assert feedback.status_code == 200

        resumed = client.post(f"/api/research/{task_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "running"

        cancelled = client.post(f"/api/research/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
