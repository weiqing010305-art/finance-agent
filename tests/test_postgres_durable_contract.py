from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.durable import DurableConflict, PostgresDurableRepository
from backend.db.metadata import (
    memberships, metadata, research_checkpoints_pg, research_steps_pg, tenants, users,
)


def _repo():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine); now = datetime.now(timezone.utc)
    with engine.begin() as c:
        for uid, tid in (("u1", "t1"), ("u2", "t2")):
            c.execute(users.insert().values(id=uid, email=f"{uid}@example.com", password_hash="x", created_at=now))
            c.execute(tenants.insert().values(id=tid, name=tid, created_at=now))
            c.execute(memberships.insert().values(tenant_id=tid, user_id=uid, role="owner"))
    return engine, PostgresDurableRepository(engine), PrincipalContext("u1", "t1", "owner")


def test_atomic_create_has_plan_checkpoint_lease_and_stable_idempotency():
    engine, repo, principal = _repo()
    created = repo.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="k",
        plan={"steps": [{"id": "s1"}]}, owner_id="worker",
    )
    assert created.created and created.lease_token
    replay = repo.create_run(
        principal, company="Tencent", question="cash flow", idempotency_key="k",
        plan={"steps": [{"id": "s1"}]}, owner_id="worker2",
    )
    assert replay.run_id == created.run_id and not replay.created and replay.lease_token == ""
    with engine.connect() as c:
        assert c.scalar(select(research_checkpoints_pg.c.version)) == 1


def test_idempotency_identity_and_cross_tenant_access_fail_closed():
    _, repo, principal = _repo()
    created = repo.create_run(
        principal, company="Tencent", question="q", idempotency_key="k", plan={"steps": []}, owner_id="w",
    )
    with pytest.raises(DurableConflict, match="different request"):
        repo.create_run(principal, company="Other", question="q", idempotency_key="k", plan={"steps": []}, owner_id="w")
    assert repo.get_run(PrincipalContext("u2", "t2", "owner"), created.run_id) is None


def test_step_commit_is_atomic_idempotent_and_pause_safe():
    engine, repo, principal = _repo()
    created = repo.create_run(
        principal, company="Tencent", question="q", idempotency_key="k", plan={"steps": ["s1"]}, owner_id="w",
    )
    repo.transition(principal, created.run_id, from_status="running", to_status="pause_requested", expected_version=0)
    snapshot = repo.commit_step(
        principal, created.run_id, lease_token=created.lease_token, step_id="s1",
        step_input={"q": "x"}, step_output={"answer": 1}, next_pointer="s2",
        progress=30, budget_delta=2,
    )
    assert snapshot["run"]["status"] == "paused"
    assert snapshot["checkpoint"]["next_pointer"] == "s2"
    with engine.connect() as c:
        assert c.scalar(select(research_steps_pg.c.step_id)) == "s1"
    with pytest.raises(DurableConflict, match="lease lost"):
        repo.commit_step(
            principal, created.run_id, lease_token=created.lease_token, step_id="s2",
            step_input={}, step_output={}, next_pointer="s3", progress=40, budget_delta=1,
        )


def test_replayed_completed_step_acknowledges_a_late_pause_request():
    _, repo, principal = _repo()
    created = repo.create_run(
        principal, company="Tencent", question="q", idempotency_key="late-pause",
        plan={"steps": ["s1"]}, owner_id="w",
    )
    operation = dict(
        lease_token=created.lease_token, step_id="s1", step_input={"q": "x"},
        step_output={"answer": 1}, next_pointer="report", progress=60, budget_delta=0,
    )
    repo.commit_step(principal, created.run_id, **operation)
    repo.transition(
        principal, created.run_id, from_status="running", to_status="pause_requested",
        expected_version=0,
    )
    replay = repo.commit_step(principal, created.run_id, **operation)
    assert replay["run"]["status"] == "paused"


def test_illegal_or_stale_transition_does_not_mutate_state():
    _, repo, principal = _repo()
    created = repo.create_run(
        principal, company="Tencent", question="q", idempotency_key="k", plan={"steps": []}, owner_id="w",
    )
    with pytest.raises(DurableConflict, match="illegal"):
        repo.transition(principal, created.run_id, from_status="running", to_status="paused", expected_version=0)
    repo.transition(principal, created.run_id, from_status="running", to_status="pause_requested", expected_version=0)
    with pytest.raises(DurableConflict, match="state version"):
        repo.transition(principal, created.run_id, from_status="running", to_status="failed", expected_version=0)
