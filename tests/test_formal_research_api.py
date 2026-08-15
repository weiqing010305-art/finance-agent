from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.durable import PostgresDurableRepository
from backend.db.artifacts import PostgresResearchArtifacts
from backend.db.metadata import memberships, metadata, tenants, users
from backend.formal_research_api import build_formal_research_router
from backend.jobs.ledger import JobLedger


def _app(sender=None, *, execution_profile="synthetic_smoke"):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(users.insert().values(
            id="u1", email="u1@example.com", password_hash="x", created_at=now,
        ))
        connection.execute(tenants.insert().values(id="t1", name="T1", created_at=now))
        connection.execute(memberships.insert().values(tenant_id="t1", user_id="u1", role="owner"))
    principal = PrincipalContext("u1", "t1", "owner")
    sent: list[str] = []
    durable = PostgresDurableRepository(engine)
    app = FastAPI()
    app.include_router(build_formal_research_router(
        durable, JobLedger(engine), PostgresResearchArtifacts(engine),
        can_create=lambda: principal, can_read=lambda: principal, sender=sender or sent.append,
        execution_profile=execution_profile,
    ))
    app.state.durable = durable
    app.state.principal = principal
    return TestClient(app), sent


def test_formal_research_create_is_atomic_dispatched_and_idempotent():
    client, sent = _app()
    payload = {
        "company": "腾讯控股", "symbol": "0700", "market": "HK",
        "question": "分析腾讯现金流质量", "depth": "quick", "budget_limit": 20,
    }
    headers = {"Idempotency-Key": "formal-request-0001"}
    first = client.post("/api/research", json=payload, headers=headers)
    assert first.status_code == 202
    body = first.json()
    assert body["execution_profile"] == "synthetic_smoke"
    assert body["dispatch_status"] == "published"
    assert sent == [body["run_id"]]
    replay = client.post("/api/research", json=payload, headers=headers)
    assert replay.status_code == 202 and replay.json()["run_id"] == body["run_id"]
    assert replay.json()["created"] is False and sent == [body["run_id"]]
    viewed = client.get(f"/api/research/{body['run_id']}")
    assert viewed.status_code == 200 and viewed.json()["company"] == "腾讯控股"


def test_broker_failure_keeps_transactional_outbox_pending():
    def fail(_job_id):
        raise ConnectionError("redis down")

    client, _ = _app(fail)
    response = client.post("/api/research", json={
        "company": "腾讯控股", "symbol": "0700", "market": "HK",
        "question": "分析腾讯盈利能力", "depth": "quick", "budget_limit": 20,
    }, headers={"Idempotency-Key": "formal-request-0002"})
    assert response.status_code == 202
    assert response.json()["dispatch_status"] == "outbox_pending"
    assert "lease_token" not in response.json() and "claim_token" not in response.json()


def test_real_rag_profile_persists_minimal_matching_plan_and_job_kind():
    client, sent = _app(execution_profile="real_rag_local")
    response = client.post("/api/research", json={
        "company": "腾讯控股", "symbol": "0700", "market": "HK",
        "question": "分析腾讯现金流质量", "depth": "quick", "budget_limit": 2,
    }, headers={"Idempotency-Key": "formal-real-rag-0001"})
    assert response.status_code == 202
    body = response.json()
    assert body["execution_profile"] == "real_rag_local"
    assert sent == [body["run_id"]]
    plan = client.app.state.durable.get_latest_plan(
        client.app.state.principal, body["run_id"]
    )
    assert plan["execution_profile"] == "real_rag_local"
    assert [step["id"] for step in plan["steps"]] == [
        "retrieve_documents", "synthesize_verified_report",
    ]
    assert "proposed_external_plan" not in plan
    viewed = client.get(f"/api/research/{body['run_id']}").json()
    assert viewed["execution_profile"] == "real_rag_local"


def test_owner_can_retry_a_failed_run_as_a_new_idempotent_run():
    client, sent = _app()
    created = client.post("/api/research", json={
        "company": "Tencent", "symbol": "0700", "market": "HK",
        "question": "validate durable retry behavior", "depth": "quick", "budget_limit": 20,
    }, headers={"Idempotency-Key": "failed-original-01"}).json()
    client.app.state.durable.transition(
        client.app.state.principal, created["run_id"], from_status="running",
        to_status="failed", expected_version=0,
    )
    retried = client.post(
        f"/api/research/{created['run_id']}/retry",
        headers={"Idempotency-Key": "failed-retry-0001"},
    )
    assert retried.status_code == 202
    assert retried.json()["run_id"] != created["run_id"]
    assert retried.json()["retried_from"] == created["run_id"]
    assert sent == [created["run_id"], retried.json()["run_id"]]


def _historical_run(client, *, profile: str, key: str):
    return client.app.state.durable.create_run(
        client.app.state.principal,
        company="Tencent",
        question="resume a historical execution profile",
        idempotency_key=key,
        plan={
            "version": 1,
            "goal": "resume a historical execution profile",
            "steps": [{"id": "historical_step"}],
            "execution_profile": profile,
        },
        owner_id="historical-worker",
        enqueue_kind=f"{profile}_research",
    )


def test_real_runtime_can_resume_a_historical_synthetic_run():
    client, sent = _app(execution_profile="real_rag_local")
    created = _historical_run(client, profile="synthetic_smoke", key="historical-synthetic")
    repo = client.app.state.durable
    principal = client.app.state.principal
    repo.transition(
        principal, created.run_id, from_status="running", to_status="pause_requested",
        expected_version=0,
    )
    repo.transition(
        principal, created.run_id, from_status="pause_requested", to_status="paused",
        expected_version=1,
    )

    response = client.post(f"/api/research/{created.run_id}/resume")

    assert response.status_code == 202
    assert response.json()["execution_profile"] == "synthetic_smoke"
    assert len(sent) == 1


def test_synthetic_runtime_rejects_resume_and_retry_of_historical_real_runs():
    client, _ = _app(execution_profile="synthetic_smoke")
    repo = client.app.state.durable
    principal = client.app.state.principal

    paused = _historical_run(client, profile="real_rag_local", key="historical-real-paused")
    repo.transition(
        principal, paused.run_id, from_status="running", to_status="pause_requested",
        expected_version=0,
    )
    repo.transition(
        principal, paused.run_id, from_status="pause_requested", to_status="paused",
        expected_version=1,
    )
    rejected_resume = client.post(f"/api/research/{paused.run_id}/resume")
    assert rejected_resume.status_code == 409
    assert "switch runtime profile" in rejected_resume.json()["detail"]

    failed = _historical_run(client, profile="real_rag_local", key="historical-real-failed")
    repo.transition(
        principal, failed.run_id, from_status="running", to_status="failed",
        expected_version=0,
    )
    rejected_retry = client.post(
        f"/api/research/{failed.run_id}/retry",
        headers={"Idempotency-Key": "historical-real-retry"},
    )
    assert rejected_retry.status_code == 409
    assert "switch runtime profile" in rejected_retry.json()["detail"]
