from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Thread

from sqlalchemy import Engine, and_, select, text

from backend.auth.models import PrincipalContext
from backend.auth.policy import require_capability
from backend.db.durable import PostgresDurableRepository
from backend.db.metadata import jobs, memberships
from backend.jobs.ledger import JobLedger


JobHandler = Callable[[PrincipalContext, str, str], None]


class JobContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerJobContextResolver:
    engine: Engine

    def resolve(self, job_id: str) -> PrincipalContext:
        with self.engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                row = connection.execute(text(
                    "SELECT tenant_id, user_id, role FROM public.finscope_job_context(:job_id)"
                ), {"job_id": job_id}).mappings().one_or_none()
            else:
                row = connection.execute(select(
                    jobs.c.tenant_id, jobs.c.created_by.label("user_id"), memberships.c.role,
                ).join(memberships, and_(
                    memberships.c.tenant_id == jobs.c.tenant_id,
                    memberships.c.user_id == jobs.c.created_by,
                )).where(jobs.c.id == job_id)).mappings().one_or_none()
        if row is None:
            raise JobContextError("persisted job context not found")
        return PrincipalContext(
            tenant_id=str(row["tenant_id"]), user_id=str(row["user_id"]), role=str(row["role"]),
        )


class PersistedJobExecutor:
    """Claims a PostgreSQL-ledger job, then exchanges that claim for a run lease."""

    def __init__(
        self, *, resolver: WorkerJobContextResolver, ledger: JobLedger,
        durable: PostgresDurableRepository, handlers: dict[str, JobHandler], owner_id: str,
        heartbeat_interval_seconds: float = 10.0,
    ):
        self.resolver = resolver
        self.ledger = ledger
        self.durable = durable
        self.handlers = dict(handlers)
        self.owner_id = owner_id
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def __call__(self, job_id: str) -> None:
        principal = self.resolver.resolve(job_id)
        claim = self.ledger.claim(principal, job_id)
        if claim is None:
            return
        try:
            require_capability(principal, "research.create")
            handler = self.handlers.get(claim.kind)
            if handler is None:
                raise JobContextError("persisted job kind is not registered")
            run_id = str(claim.payload.get("run_id", ""))
            if not run_id:
                raise JobContextError("persisted job identity mismatch")
            lease_token = self.durable.acquire_run_lease_for_job(
                principal, run_id, job_id=claim.job_id,
                job_claim_token=claim.token, owner_id=self.owner_id,
            )
            stop, lost = Event(), Event()

            def heartbeat() -> None:
                while not stop.wait(self.heartbeat_interval_seconds):
                    job_ok = self.ledger.heartbeat(principal, claim)
                    run_ok = self.durable.renew_run_lease(
                        principal, run_id, lease_token=lease_token,
                    )
                    if job_ok and run_ok:
                        continue
                    run = self.durable.get_run(principal, run_id)
                    if not job_ok or run is None or run.get("status") not in {
                        "paused", "completed", "failed",
                    }:
                        lost.set()
                    return

            pulse = Thread(target=heartbeat, name=f"job-heartbeat-{job_id}", daemon=True)
            pulse.start()
            try:
                handler(principal, run_id, lease_token)
            finally:
                stop.set()
                pulse.join(timeout=max(1.0, self.heartbeat_interval_seconds * 2))
            if lost.is_set():
                raise JobContextError("job or run lease heartbeat was lost")
            if not self.ledger.complete(principal, claim):
                raise JobContextError("job claim was lost before completion")
        except Exception as exc:
            self.ledger.fail(principal, claim, str(exc))
            raise
