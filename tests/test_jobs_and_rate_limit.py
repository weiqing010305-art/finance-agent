from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.durable import PostgresDurableRepository
from backend.db.metadata import job_outbox, jobs, metadata, research_events_pg
from backend.jobs.dispatch import GlobalOutboxDispatcher
from backend.jobs.ledger import JobLedger
from backend.rate_limit import RedisSlidingWindowLimiter


def _ledger():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    # SQLite contract tests use FK-free fixtures; PostgreSQL integration proves FK/RLS.
    engine.execute if False else None
    return engine, JobLedger(engine, claim_ttl_seconds=1), PrincipalContext("u", "t", "owner")


def _seed_identity(engine):
    from backend.db.metadata import memberships, tenants, users
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(users.insert().values(id="u", email="u@example.com", password_hash="x", created_at=now))
        c.execute(tenants.insert().values(id="t", name="T", created_at=now))
        c.execute(memberships.insert().values(tenant_id="t", user_id="u", role="owner"))


def test_job_duplicate_delivery_and_stale_worker_are_fenced():
    engine, ledger, principal = _ledger(); _seed_identity(engine)
    job_id = ledger.enqueue(principal, kind="ingest", payload={"object_id": "o"})
    first = ledger.claim(principal, job_id)
    assert first is not None and ledger.claim(principal, job_id) is None
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.id == job_id).values(
            claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        ))
    second = ledger.claim(principal, job_id)
    assert second is not None and second.token != first.token
    assert ledger.complete(principal, first) is False
    assert ledger.complete(principal, second) is True


def test_outbox_survives_broker_loss_until_marked_published():
    engine, ledger, principal = _ledger(); _seed_identity(engine)
    job_id = ledger.enqueue(principal, kind="x", payload={})
    assert ledger.unpublished(principal) == [job_id]
    assert ledger.mark_published(principal, job_id) is True
    assert ledger.unpublished(principal) == []


def test_global_dispatcher_republishes_expired_claim_without_exposing_payload():
    engine, ledger, principal = _ledger(); _seed_identity(engine)
    job_id = ledger.enqueue(principal, kind="research", payload={"private": "body"})
    sent = []
    dispatcher = GlobalOutboxDispatcher(engine, ledger, sent.append)
    assert dispatcher.publish_due() == 1 and sent == [job_id]
    claim = ledger.claim(principal, job_id)
    assert claim is not None
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.id == job_id).values(claim_expires_at=old))
        c.execute(update(job_outbox).where(job_outbox.c.job_id == job_id).values(published_at=old))
    due = dispatcher.due()
    assert len(due) == 1 and not hasattr(due[0], "payload")
    assert dispatcher.publish_due() == 1 and sent == [job_id, job_id]


def test_global_dispatcher_republishes_a_published_but_unclaimed_delivery():
    engine, ledger, principal = _ledger(); _seed_identity(engine)
    job_id = ledger.enqueue(principal, kind="research", payload={"private": "body"})
    assert ledger.mark_published(principal, job_id)
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    with engine.begin() as c:
        c.execute(update(job_outbox).where(job_outbox.c.job_id == job_id).values(published_at=old))
    sent = []
    dispatcher = GlobalOutboxDispatcher(engine, ledger, sent.append)
    assert [item.job_id for item in dispatcher.due()] == [job_id]
    assert dispatcher.publish_due() == 1 and sent == [job_id]


def test_final_expired_claim_is_dead_lettered_instead_of_staying_running():
    engine, ledger, principal = _ledger(); _seed_identity(engine)
    job_id = ledger.enqueue(principal, kind="research", payload={}, max_attempts=1)
    claim = ledger.claim(principal, job_id)
    assert claim is not None
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.id == job_id).values(
            claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        ))
    dispatcher = GlobalOutboxDispatcher(engine, ledger, lambda _: None)
    assert dispatcher.reconcile_exhausted() == 1
    with engine.connect() as c:
        assert c.scalar(select(jobs.c.status).where(jobs.c.id == job_id)) == "dead"


def test_retry_failure_reopens_outbox_only_when_an_attempt_remains():
    engine, ledger, principal = _ledger(); _seed_identity(engine)
    job_id = ledger.enqueue(principal, kind="research", payload={})
    ledger.mark_published(principal, job_id)
    claim = ledger.claim(principal, job_id)
    assert ledger.fail(principal, claim, "temporary") is True
    with engine.connect() as c:
        assert c.scalar(select(job_outbox.c.published_at)) is None


def test_terminal_handler_failure_atomically_dead_letters_job_and_fails_run():
    engine, _, principal = _ledger(); _seed_identity(engine)
    durable = PostgresDurableRepository(engine)
    created = durable.create_run(
        principal, company="Tencent", question="cash", idempotency_key="terminal-fail",
        plan={"steps": [{"id": "smoke"}]}, owner_id="api",
        enqueue_kind="synthetic_smoke_research", max_attempts=1,
    )
    ledger = JobLedger(engine)
    claim = ledger.claim(principal, created.job_id)
    assert claim is not None and ledger.fail(principal, claim, "provider token=secret failed")
    with engine.connect() as connection:
        assert connection.scalar(select(jobs.c.status).where(jobs.c.id == created.job_id)) == "dead"
        run = durable.get_run(principal, created.run_id)
        assert run["status"] == "failed"
        assert connection.scalar(select(research_events_pg.c.event_type).where(
            research_events_pg.c.run_id == created.run_id,
        ).order_by(research_events_pg.c.created_at.desc())) == "run.failed"


class BrokenRedis:
    def eval(self, *args):
        raise ConnectionError("down")


def test_rate_limit_redis_failure_closes_costly_writes_but_can_degrade_reads():
    limiter = RedisSlidingWindowLimiter(BrokenRedis())
    assert limiter.check(scope="research", identity="u", limit=1, window_seconds=60, fail_closed=True).allowed is False
    read = limiter.check(scope="read", identity="u", limit=1, window_seconds=60, fail_closed=False)
    assert read.allowed is True and read.degraded is True
