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
    if os.getenv("FINSCOPE_JOB_CONSUMER") != "1":
        return
    if os.getenv("FINSCOPE_RUNTIME_MODE") != "local":
        return
    if os.getenv("DATABASE_ROLE") != "finscope_worker":
        return
    profile = os.getenv("FINSCOPE_FORMAL_EXECUTOR", "").strip()
    if profile not in {"synthetic_smoke", "real_rag_local", "controlled_tools"}:
        raise RuntimeError("formal worker requires an explicitly supported executor profile")

    from sqlalchemy import create_engine

    from backend.db.artifacts import PostgresResearchArtifacts
    from backend.db.durable import PostgresDurableRepository
    from backend.formal_processor import (
        ControlledToolsResearchProcessor,
        FormalRealRagProcessor,
        SyntheticSmokeResearchProcessor,
    )
    from backend.jobs.executor import PersistedJobExecutor, WorkerJobContextResolver
    from backend.jobs.ledger import JobLedger
    from backend.jobs.worker_runtime import configure_executor
    from backend.settings import RuntimeSettings

    settings = RuntimeSettings.from_env()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    durable = PostgresDurableRepository(engine)
    artifacts = PostgresResearchArtifacts(engine)
    synthetic_processor = SyntheticSmokeResearchProcessor(durable, artifacts)
    handlers = {"synthetic_smoke_research": synthetic_processor}
    if profile == "controlled_tools":
        handlers["controlled_tools_research"] = ControlledToolsResearchProcessor(durable, artifacts)
    if profile == "real_rag_local":
        from backend.authorized_retrieval import AuthorizedChunkCatalog, AuthorizedMilvusRetriever
        from backend.embeddings import BgeLargeZhEmbeddingProvider
        from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever

        embeddings = BgeLargeZhEmbeddingProvider(device=settings.bge_device)
        embeddings.runtime_metadata()  # fail startup if the pinned model is unavailable
        milvus = MilvusHybridRetriever(MilvusConfig(
            uri=settings.milvus_uri, token=None, collection=settings.milvus_collection,
        ), embeddings)
        milvus.ensure_collection()  # fail startup on an incompatible/unreachable index
        real_processor = FormalRealRagProcessor(
            durable, artifacts,
            AuthorizedMilvusRetriever(AuthorizedChunkCatalog(engine), milvus),
            embedding_profile_id=embeddings.profile.profile_id,
            index_version=settings.rag_index_version,
        )
        handlers["real_rag_local_research"] = real_processor
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers=handlers,
        owner_id=f"worker:{socket.gethostname()}:{os.getpid()}",
    )
    configure_executor(executor)


_configure_formal_worker()
