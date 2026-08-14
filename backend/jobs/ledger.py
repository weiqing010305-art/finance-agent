from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import Engine, and_, delete, insert, or_, select, update

from backend.auth.models import PrincipalContext
from backend.db.metadata import (
    job_outbox, jobs, research_events_pg, research_leases_pg, research_runs_pg,
)
from backend.db.session import principal_transaction
from backend.redaction import redact_text


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class JobClaim:
    job_id: str
    token: str
    tenant_id: str
    kind: str
    payload: dict
    attempt: int


class JobLedger:
    def __init__(self, engine: Engine, *, claim_ttl_seconds: int = 60):
        self.engine, self.claim_ttl_seconds = engine, claim_ttl_seconds

    def enqueue(
        self, principal: PrincipalContext, *, kind: str, payload: dict, max_attempts: int = 3,
    ) -> str:
        now, job_id = _now(), str(uuid4())
        with principal_transaction(self.engine, principal) as connection:
            connection.execute(insert(jobs).values(
                id=job_id, tenant_id=principal.tenant_id, created_by=principal.user_id,
                kind=kind, payload_json=json.dumps(payload, sort_keys=True), status="pending",
                max_attempts=max_attempts, next_attempt_at=now, created_at=now, updated_at=now,
            ))
            connection.execute(insert(job_outbox).values(
                id=str(uuid4()), tenant_id=principal.tenant_id, job_id=job_id, created_at=now,
            ))
        return job_id

    def unpublished(self, principal: PrincipalContext, *, limit: int = 100) -> list[str]:
        with principal_transaction(self.engine, principal) as connection:
            return list(connection.scalars(select(job_outbox.c.job_id).where(
                and_(job_outbox.c.tenant_id == principal.tenant_id, job_outbox.c.published_at.is_(None))
            ).limit(limit)))

    def mark_published(self, principal: PrincipalContext, job_id: str) -> bool:
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(job_outbox).where(and_(
                job_outbox.c.job_id == job_id,
                job_outbox.c.tenant_id == principal.tenant_id,
            )).values(published_at=_now()))
            return result.rowcount == 1

    def claim(self, principal: PrincipalContext, job_id: str) -> JobClaim | None:
        now, token = _now(), secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=self.claim_ttl_seconds)
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(jobs).where(and_(
                jobs.c.id == job_id,
                jobs.c.tenant_id == principal.tenant_id,
                or_(
                    and_(jobs.c.status.in_(("pending", "retry")), jobs.c.next_attempt_at <= now),
                    and_(jobs.c.status == "running", jobs.c.claim_expires_at < now),
                ),
                jobs.c.attempt < jobs.c.max_attempts,
            )).values(
                status="running", attempt=jobs.c.attempt + 1, claim_token_hash=_hash(token),
                claim_expires_at=expires, updated_at=now,
            ))
            if result.rowcount != 1:
                return None
            row = connection.execute(select(jobs).where(and_(
                jobs.c.id == job_id, jobs.c.tenant_id == principal.tenant_id,
            ))).mappings().one()
        return JobClaim(job_id, token, row["tenant_id"], row["kind"], json.loads(row["payload_json"]), row["attempt"])

    def heartbeat(self, principal: PrincipalContext, claim: JobClaim) -> bool:
        now = _now()
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(jobs).where(and_(
                jobs.c.id == claim.job_id, jobs.c.status == "running",
                jobs.c.tenant_id == principal.tenant_id,
                jobs.c.claim_token_hash == _hash(claim.token), jobs.c.claim_expires_at >= now,
            )).values(claim_expires_at=now + timedelta(seconds=self.claim_ttl_seconds), updated_at=now))
            return result.rowcount == 1

    def complete(self, principal: PrincipalContext, claim: JobClaim) -> bool:
        return self._finish(principal, claim, status="completed", error=None)

    def fail(self, principal: PrincipalContext, claim: JobClaim, error: str) -> bool:
        now = _now()
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(
                jobs.c.attempt, jobs.c.max_attempts, jobs.c.payload_json,
            ).where(and_(
                jobs.c.id == claim.job_id, jobs.c.status == "running",
                jobs.c.tenant_id == principal.tenant_id,
                jobs.c.claim_token_hash == _hash(claim.token), jobs.c.claim_expires_at >= now,
            ))).one_or_none()
            if row is None:
                return False
            terminal = row.attempt >= row.max_attempts
            safe_error = redact_text(error)[:2000]
            result = connection.execute(update(jobs).where(and_(
                jobs.c.id == claim.job_id, jobs.c.status == "running",
                jobs.c.tenant_id == principal.tenant_id,
                jobs.c.claim_token_hash == _hash(claim.token),
            )).values(
                status="dead" if terminal else "retry", claim_token_hash=None,
                claim_expires_at=None, next_attempt_at=now + timedelta(seconds=min(300, 2 ** row.attempt)),
                last_error=safe_error, updated_at=now,
            ))
            if result.rowcount == 1 and not terminal:
                connection.execute(update(job_outbox).where(and_(
                    job_outbox.c.job_id == claim.job_id,
                    job_outbox.c.tenant_id == principal.tenant_id,
                )).values(published_at=None))
            if result.rowcount == 1 and terminal:
                run_id = str(json.loads(row.payload_json).get("run_id", ""))
                if run_id:
                    failed = connection.execute(update(research_runs_pg).where(and_(
                        research_runs_pg.c.id == run_id,
                        research_runs_pg.c.tenant_id == principal.tenant_id,
                        research_runs_pg.c.status.in_(("running", "pause_requested", "resuming")),
                    )).values(
                        status="failed", state_version=research_runs_pg.c.state_version + 1,
                        updated_at=now,
                    ))
                    if failed.rowcount == 1:
                        connection.execute(delete(research_leases_pg).where(and_(
                            research_leases_pg.c.run_id == run_id,
                            research_leases_pg.c.tenant_id == principal.tenant_id,
                        )))
                        connection.execute(insert(research_events_pg).values(
                            id=str(uuid4()), run_id=run_id, tenant_id=principal.tenant_id,
                            event_type="run.failed",
                            payload_json=json.dumps({"reason": "job_attempts_exhausted"}),
                            created_at=now,
                        ))
            return result.rowcount == 1

    def _finish(self, principal: PrincipalContext, claim: JobClaim, *, status: str, error: str | None) -> bool:
        now = _now()
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(jobs).where(and_(
                jobs.c.id == claim.job_id, jobs.c.status == "running",
                jobs.c.tenant_id == principal.tenant_id,
                jobs.c.claim_token_hash == _hash(claim.token), jobs.c.claim_expires_at >= now,
            )).values(
                status=status, claim_token_hash=None, claim_expires_at=None,
                last_error=error, updated_at=now,
            ))
            return result.rowcount == 1
