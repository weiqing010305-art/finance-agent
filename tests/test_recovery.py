from __future__ import annotations

import time
from datetime import timedelta

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import Repository
from backend.durable_runner import DurableRunner
from backend.schemas import ResearchCreate


def request() -> ResearchCreate:
    return ResearchCreate(company="腾讯控股", question="分析腾讯近三年的盈利质量")


def expire(repository: Repository, run_id: str) -> None:
    with repository.connect() as connection:
        connection.execute(
            "UPDATE run_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run_id,),
        )


def test_reconciler_takes_over_expired_run_and_preserves_checkpoint(tmp_path):
    repository = Repository(tmp_path / "recover.db")
    repository.initialize()
    first = DurableRunner(repository, lease_ttl=timedelta(seconds=30))
    created = first.create_run(request(), owner_id="old", idempotency_key="recover")
    first.commit_step(
        created.run["id"],
        lease_token=created.lease_token,
        step_id="planning",
        kind="planning",
        step_input={},
        step_output={"ok": True},
        idempotency_key="mock:planning",
        frontier={
            "plan_version": 1,
            "ready_step_ids": ["searching"],
            "running_step_ids": [],
            "blocked_step_ids": [],
            "completed_step_ids": ["planning"],
        },
        progress=12,
    )
    expire(repository, created.run["id"])

    second = DurableRunner(repository)
    recovered = second.reconcile_expired_runs(owner_id="new")

    assert len(recovered) == 1
    assert recovered[0].run["status"] == "running"
    assert recovered[0].lease_token != created.lease_token
    snapshot = repository.get_runtime_snapshot(created.run["id"])
    assert snapshot["checkpoint"]["sequence"] == 1
    assert snapshot["checkpoint"]["frontier"]["completed_step_ids"] == ["planning"]
    assert snapshot["run"]["recovery_required"] is False
    assert [event["kind"] for event in snapshot["events"]][-2:] == [
        "run.resuming",
        "run.running",
    ]


def test_corrupt_checkpoint_is_failed_during_reconciliation(tmp_path):
    repository = Repository(tmp_path / "corrupt.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(request(), owner_id="old", idempotency_key="corrupt")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE checkpoints SET frontier_json = 'not-json' WHERE run_id = ?",
            (created.run["id"],),
        )
    expire(repository, created.run["id"])

    assert runner.reconcile_expired_runs(owner_id="new") == []
    failed = repository.get_task(created.run["id"])
    assert failed["status"] == "failed"
    assert "Recovery validation failed" in failed["error"]


def test_reconciler_preserves_pending_pause_intent(tmp_path):
    repository = Repository(tmp_path / "pause-recovery.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(request(), owner_id="old", idempotency_key="pause-recovery")
    runner.request_pause(created.run["id"])
    expire(repository, created.run["id"])

    recovered = runner.reconcile_expired_runs(owner_id="new")

    assert recovered == []
    assert repository.get_task(created.run["id"])["status"] == "paused"
    assert repository.get_runtime_snapshot(created.run["id"])["lease"] is None


def test_app_startup_resumes_mock_from_last_committed_step_without_duplicate(tmp_path):
    path = tmp_path / "startup.db"
    repository = Repository(path)
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(request(), owner_id="old", idempotency_key="startup")
    runner.commit_step(
        created.run["id"],
        lease_token=created.lease_token,
        step_id="planning",
        kind="planning",
        step_input={},
        step_output={"ok": True},
        idempotency_key="mock:planning",
        frontier={
            "plan_version": 1,
            "ready_step_ids": ["searching"],
            "running_step_ids": [],
            "blocked_step_ids": [],
            "completed_step_ids": ["planning"],
        },
        progress=12,
    )
    expire(repository, created.run["id"])

    app = create_app(path, mock_delay=0)
    with TestClient(app) as client:
        for _ in range(100):
            task = client.get(f"/api/research/{created.run['id']}").json()
            if task["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert task["status"] == "completed"

    snapshot = repository.get_runtime_snapshot(created.run["id"])
    assert snapshot["counts"]["steps"] == 5
    assert [event["kind"] for event in snapshot["events"]].count("step.completed") == 5


def test_paused_run_can_resume_after_app_restart(tmp_path):
    path = tmp_path / "paused-restart.db"
    first_app = create_app(path, mock_delay=0.1)
    with TestClient(first_app) as client:
        task = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        ).json()
        client.post(f"/api/research/{task['id']}/pause")
        for _ in range(100):
            paused = client.get(f"/api/research/{task['id']}").json()
            if paused["status"] == "paused":
                break
            time.sleep(0.01)
        assert paused["status"] == "paused"

    second_app = create_app(path, mock_delay=0)
    with TestClient(second_app) as client:
        assert client.get(f"/api/research/{task['id']}").json()["status"] == "paused"
        assert client.post(f"/api/research/{task['id']}/resume").status_code == 200
        completed = None
        for _ in range(100):
            completed = client.get(f"/api/research/{task['id']}").json()
            if completed["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert completed["status"] == "completed"


def test_manual_resume_rejects_corrupt_checkpoint_and_does_not_spawn_worker(tmp_path):
    path = tmp_path / "manual-corrupt.db"
    app = create_app(path, mock_delay=0.1)
    with TestClient(app) as client:
        task = client.post(
            "/api/research",
            json={"company": "腾讯控股", "question": "分析利润增长是否可持续"},
        ).json()
        client.post(f"/api/research/{task['id']}/pause")
        for _ in range(100):
            paused = client.get(f"/api/research/{task['id']}").json()
            if paused["status"] == "paused":
                break
            time.sleep(0.01)
        assert paused["status"] == "paused"
        with app.state.repository.connect() as connection:
            connection.execute(
                "UPDATE checkpoints SET frontier_json = 'not-json' WHERE run_id = ?",
                (task["id"],),
            )

        response = client.post(f"/api/research/{task['id']}/resume")
        assert response.status_code == 409
        failed = client.get(f"/api/research/{task['id']}").json()
        assert failed["status"] == "failed"
        time.sleep(0.05)
        assert client.get(f"/api/research/{task['id']}").json()["status"] == "failed"
