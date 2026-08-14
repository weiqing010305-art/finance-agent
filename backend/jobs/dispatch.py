from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from sqlalchemy import Engine, and_, delete, insert, or_, select, text, update

from backend.auth.models import PrincipalContext
from backend.db.metadata import (
    job_outbox, jobs, memberships, research_events_pg, research_leases_pg, research_runs_pg,
)
from backend.jobs.ledger import JobLedger


class OutboxDispatcher:
    """Publishes job IDs only; payload and authority stay in PostgreSQL."""
    def __init__(self, ledger: JobLedger, sender: Callable[[str], None]):
        self.ledger, self.sender = ledger, sender

    def publish_pending(self, principal: PrincipalContext, *, limit: int = 100) -> int:
        published = 0
        for job_id in self.ledger.unpublished(principal, limit=limit):
            self.sender(job_id)
            if self.ledger.mark_published(principal, job_id):
                published += 1
        return published


@dataclass(frozen=True)
class DueDelivery:
    job_id: str
    principal: PrincipalContext


class GlobalOutboxDispatcher:
    """Discovers only due delivery identities; job payload stays behind RLS."""

    def __init__(self, engine: Engine, ledger: JobLedger, sender: Callable[[str], None]):
        self.engine, self.ledger, self.sender = engine, ledger, sender

    def due(self, *, limit: int = 100) -> list[DueDelivery]:
        if not 1 <= limit <= 1000:
            raise ValueError("invalid dispatcher batch size")
        with self.engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                rows = connection.execute(text(
                    "SELECT job_id, tenant_id, user_id, role "
                    "FROM public.finscope_due_job_deliveries(:batch_limit)"
                ), {"batch_limit": limit}).mappings().all()
            else:
                now = datetime.now(timezone.utc)
                stale_delivery = now - timedelta(seconds=30)
                rows = connection.execute(select(
                    jobs.c.id.label("job_id"), jobs.c.tenant_id,
                    jobs.c.created_by.label("user_id"), memberships.c.role,
                ).join(job_outbox, job_outbox.c.job_id == jobs.c.id).join(
                    memberships, and_(memberships.c.tenant_id == jobs.c.tenant_id,
                                      memberships.c.user_id == jobs.c.created_by),
                ).where(and_(
                    jobs.c.attempt < jobs.c.max_attempts,
                    or_(
                        and_(jobs.c.status.in_(("pending", "retry")),
                             jobs.c.next_attempt_at <= now,
                             or_(job_outbox.c.published_at.is_(None),
                                 job_outbox.c.published_at < stale_delivery)),
                        and_(jobs.c.status == "running", jobs.c.claim_expires_at < now,
                             or_(job_outbox.c.published_at.is_(None),
                                 job_outbox.c.published_at < stale_delivery)),
                    ),
                )).limit(limit)).mappings().all()
        return [DueDelivery(
            job_id=str(row["job_id"]),
            principal=PrincipalContext(str(row["user_id"]), str(row["tenant_id"]), str(row["role"])),
        ) for row in rows]

    def publish_due(self, *, limit: int = 100) -> int:
        self.reconcile_exhausted(limit=limit)
        published = 0
        for delivery in self.due(limit=limit):
            self.sender(delivery.job_id)
            if self.ledger.mark_published(delivery.principal, delivery.job_id):
                published += 1
        return published

    def reconcile_exhausted(self, *, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("invalid dispatcher batch size")
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                return int(connection.scalar(text(
                    "SELECT public.finscope_reconcile_exhausted_jobs(:batch_limit)"
                ), {"batch_limit": limit}) or 0)
            now = datetime.now(timezone.utc)
            member_keys = set(connection.execute(select(
                memberships.c.tenant_id, memberships.c.user_id,
            )).all())
            candidates = connection.execute(select(jobs).where(
                jobs.c.status.in_(("pending", "retry", "running"))
            ).limit(limit)).mappings().all()
            reconciled = 0
            for job in candidates:
                expired_last_claim = (
                    job["status"] == "running" and job["claim_expires_at"] is not None
                    and job["claim_expires_at"].replace(tzinfo=timezone.utc) < now
                    and job["attempt"] >= job["max_attempts"]
                )
                revoked = (job["tenant_id"], job["created_by"]) not in member_keys
                if not (expired_last_claim or revoked):
                    continue
                connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(
                    status="dead", claim_token_hash=None, claim_expires_at=None,
                    last_error="delivery exhausted or principal revoked", updated_at=now,
                ))
                run_id = json.loads(job["payload_json"]).get("run_id")
                if run_id:
                    failed = connection.execute(update(research_runs_pg).where(and_(
                        research_runs_pg.c.id == run_id,
                        research_runs_pg.c.tenant_id == job["tenant_id"],
                        research_runs_pg.c.status.in_(("running", "pause_requested", "resuming")),
                    )).values(
                        status="failed", state_version=research_runs_pg.c.state_version + 1,
                        updated_at=now,
                    ))
                    if failed.rowcount == 1:
                        connection.execute(delete(research_leases_pg).where(and_(
                            research_leases_pg.c.run_id == run_id,
                            research_leases_pg.c.tenant_id == job["tenant_id"],
                        )))
                        connection.execute(insert(research_events_pg).values(
                            id=str(uuid4()), run_id=run_id, tenant_id=job["tenant_id"],
                            event_type="run.failed",
                            payload_json='{"reason":"job_delivery_exhausted"}', created_at=now,
                        ))
                reconciled += 1
            return reconciled
