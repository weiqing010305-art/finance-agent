from __future__ import annotations

import os
import socket

import dramatiq
from dramatiq.brokers.redis import RedisBroker


redis_broker = RedisBroker(url=os.getenv("REDIS_URL", "redis://redis:6379/0"))
dramatiq.set_broker(redis_broker)


@dramatiq.actor(max_retries=0, queue_name="finscope")
def execute_job(job_id: str) -> None:
    # The message deliberately carries no payload or authority. A production
    # handler resolves job_id through PostgreSQL, claims it with fencing, then
    # dispatches the persisted kind. Task-specific handlers are registered as
    # their PostgreSQL aggregates are ported.
    from backend.jobs.worker_runtime import execute_persisted_job
    execute_persisted_job(job_id)


def _configure_formal_worker() -> None:
    if os.getenv("FINSCOPE_RUNTIME_MODE") != "local":
        return
    if os.getenv("DATABASE_ROLE") != "finscope_worker":
        return
    profile = os.getenv("FINSCOPE_FORMAL_EXECUTOR", "").strip()
    if profile != "synthetic_smoke":
        raise RuntimeError("formal worker requires an explicitly supported executor profile")

    from sqlalchemy import create_engine

    from backend.db.artifacts import PostgresResearchArtifacts
    from backend.db.durable import PostgresDurableRepository
    from backend.formal_processor import SyntheticSmokeResearchProcessor
    from backend.jobs.executor import PersistedJobExecutor, WorkerJobContextResolver
    from backend.jobs.ledger import JobLedger
    from backend.jobs.worker_runtime import configure_executor
    from backend.settings import RuntimeSettings

    settings = RuntimeSettings.from_env()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    durable = PostgresDurableRepository(engine)
    artifacts = PostgresResearchArtifacts(engine)
    processor = SyntheticSmokeResearchProcessor(durable, artifacts)
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers={"synthetic_smoke_research": processor},
        owner_id=f"worker:{socket.gethostname()}:{os.getpid()}",
    )
    configure_executor(executor)


_configure_formal_worker()
