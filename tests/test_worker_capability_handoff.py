from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.artifacts import PostgresResearchArtifacts
from backend.db.durable import DurableConflict, PostgresDurableRepository
from backend.db.metadata import evidence_items_pg, jobs, memberships, metadata, reports_pg, tenants, users
from backend.formal_processor import SyntheticSmokeResearchProcessor
from backend.jobs.executor import PersistedJobExecutor, WorkerJobContextResolver
from backend.jobs.ledger import JobLedger


def _runtime():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(users.insert().values(
            id="u1", email="u1@example.com", password_hash="x", created_at=now,
        ))
        connection.execute(tenants.insert().values(id="t1", name="T1", created_at=now))
        connection.execute(memberships.insert().values(tenant_id="t1", user_id="u1", role="owner"))
    return engine, PrincipalContext("u1", "t1", "owner")


def test_job_claim_is_required_to_exchange_for_run_lease():
    engine, principal = _runtime()
    durable = PostgresDurableRepository(engine)
    created = durable.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="request-key",
        plan={"steps": [{"id": "smoke"}]}, owner_id="api",
        enqueue_kind="synthetic_smoke_research",
    )
    with pytest.raises(DurableConflict, match="job claim"):
        durable.acquire_run_lease_for_job(
            principal, created.run_id, job_id=created.job_id,
            job_claim_token="forged", owner_id="worker",
        )
    claim = JobLedger(engine).claim(principal, created.run_id)
    assert claim is not None
    worker_lease = durable.acquire_run_lease_for_job(
        principal, created.run_id, job_id=created.job_id,
        job_claim_token=claim.token, owner_id="worker",
    )
    assert worker_lease and worker_lease != created.lease_token
    with pytest.raises(DurableConflict, match="lease lost"):
        durable.commit_step(
            principal, created.run_id, lease_token=created.lease_token, step_id="stale",
            step_input={}, step_output={}, next_pointer="x", progress=1, budget_delta=0,
        )


def test_run_lease_renewal_and_completed_step_replay_are_fenced():
    engine, principal = _runtime()
    durable = PostgresDurableRepository(engine, lease_seconds=30)
    created = durable.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="renew-replay",
        plan={"steps": [{"id": "retrieve"}]}, owner_id="api",
        enqueue_kind="synthetic_smoke_research",
    )
    claim = JobLedger(engine).claim(principal, created.job_id)
    lease = durable.acquire_run_lease_for_job(
        principal, created.run_id, job_id=created.job_id,
        job_claim_token=claim.token, owner_id="worker",
    )
    assert durable.renew_run_lease(principal, created.run_id, lease_token=lease)
    assert not durable.renew_run_lease(principal, created.run_id, lease_token="stale")
    durable.commit_step(
        principal, created.run_id, lease_token=lease, step_id="retrieve",
        step_input={"q": "cash"}, step_output={"hits": ["c1"]},
        next_pointer="report", progress=60, budget_delta=2,
    )
    assert durable.get_completed_step(principal, created.run_id, "retrieve") == {
        "input": {"q": "cash"}, "output": {"hits": ["c1"]},
    }
    assert durable.get_completed_step(principal, created.run_id, "missing") is None


def test_persisted_executor_completes_only_through_verified_artifact_gate():
    engine, principal = _runtime()
    durable = PostgresDurableRepository(engine)
    artifacts = PostgresResearchArtifacts(engine)
    created = durable.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="request-key",
        plan={"execution_profile": "synthetic_smoke", "steps": [{"id": "smoke"}]},
        owner_id="api", enqueue_kind="synthetic_smoke_research",
    )
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers={"synthetic_smoke_research": SyntheticSmokeResearchProcessor(durable, artifacts)},
        owner_id="worker:test",
    )
    executor(created.job_id)
    run = durable.get_run(principal, created.run_id)
    assert run["status"] == "completed" and run["progress"] == 100
    with engine.connect() as connection:
        assert connection.scalar(select(jobs.c.status)) == "completed"
        report = connection.execute(select(reports_pg)).mappings().one()
    assert "Synthetic smoke report" in report["markdown"]
    assert '"synthetic":true' in report["report_json"]


def test_paused_run_resumes_via_a_new_claim_fenced_job():
    engine, principal = _runtime()
    durable = PostgresDurableRepository(engine)
    processor = SyntheticSmokeResearchProcessor(durable, PostgresResearchArtifacts(engine))
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers={"synthetic_smoke_research": processor}, owner_id="worker:test",
    )
    created = durable.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="resume-key",
        plan={"steps": [{"id": "smoke"}]}, owner_id="api",
        enqueue_kind="synthetic_smoke_research",
    )
    durable.transition(
        principal, created.run_id, from_status="running", to_status="pause_requested",
        expected_version=0,
    )
    executor(created.job_id)
    paused = durable.get_run(principal, created.run_id)
    assert paused["status"] == "paused"
    resume_job = durable.resume_with_job(
        principal, created.run_id, expected_version=paused["state_version"],
        enqueue_kind="synthetic_smoke_research",
    )
    assert resume_job != created.job_id
    executor(resume_job)
    assert durable.get_run(principal, created.run_id)["status"] == "completed"


def test_pause_after_evidence_replays_idempotently_then_resume_completes():
    engine, principal = _runtime()
    durable = PostgresDurableRepository(engine)
    artifacts = PostgresResearchArtifacts(engine)
    processor = SyntheticSmokeResearchProcessor(durable, artifacts)
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers={"synthetic_smoke_research": processor}, owner_id="worker:test",
    )
    created = durable.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="pause-after-evidence",
        plan={"steps": [{"id": "smoke"}]}, owner_id="api",
        enqueue_kind="synthetic_smoke_research",
    )
    original_persist = artifacts.persist_verified_evidence
    paused_once = False

    def persist_then_pause(*args, **kwargs):
        nonlocal paused_once
        original_persist(*args, **kwargs)
        if not paused_once:
            paused_once = True
            run = durable.get_run(principal, created.run_id)
            durable.transition(
                principal, created.run_id, from_status="running", to_status="pause_requested",
                expected_version=run["state_version"],
            )

    artifacts.persist_verified_evidence = persist_then_pause
    with pytest.raises(DurableConflict):
        executor(created.job_id)
    with engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == created.job_id).values(
            next_attempt_at=datetime.now(timezone.utc),
        ))
    executor(created.job_id)
    paused = durable.get_run(principal, created.run_id)
    assert paused["status"] == "paused"
    resume_job = durable.resume_with_job(
        principal, created.run_id, expected_version=paused["state_version"],
        enqueue_kind="synthetic_smoke_research",
    )
    executor(resume_job)
    assert durable.get_run(principal, created.run_id)["status"] == "completed"
    with engine.connect() as connection:
        assert len(connection.execute(select(evidence_items_pg)).all()) == 1


def test_worker_rechecks_membership_capability_after_job_creation():
    engine, principal = _runtime()
    durable = PostgresDurableRepository(engine)
    created = durable.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="revoked-key",
        plan={"steps": [{"id": "smoke"}]}, owner_id="api",
        enqueue_kind="synthetic_smoke_research",
    )
    with engine.begin() as connection:
        connection.execute(update(memberships).values(role="viewer"))
    called = []
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers={"synthetic_smoke_research": lambda *args: called.append(args)},
        owner_id="worker:test",
    )
    with pytest.raises(PermissionError):
        executor(created.job_id)
    assert called == []
    with engine.connect() as connection:
        assert connection.scalar(select(jobs.c.status)) == "retry"
