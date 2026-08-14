from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.artifacts import PostgresResearchArtifacts
from backend.db.durable import PostgresDurableRepository
from backend.db.metadata import memberships, metadata, reports_pg, tenants, users
from backend.formal_processor import FormalRealRagProcessor
from backend.jobs.executor import PersistedJobExecutor, WorkerJobContextResolver
from backend.jobs.ledger import JobLedger
from backend.retrieval import RetrievalResponse, RetrievalResult


class FakeAuthorizedRetriever:
    def __init__(self): self.calls = 0
    def search(self, principal, request):
        self.calls += 1
        assert principal.tenant_id == "t1" and request.filters.company == "腾讯控股"
        return RetrievalResponse(backend="milvus", mode="hybrid", results=[RetrievalResult(
            chunk_id="c1", document_id="d1", document_version_id="v1",
            text="腾讯经营活动现金流持续改善。", title="本地夹具",
            source_uri="https://fixture.invalid/tencent", publisher="curated fixture",
            source_type="fixture", access_scope="public", fused_score=0.03,
            rank=1, authority_tier=2, embedding_profile_id="emb-test",
            index_version="formal-fixture-v1",
        )])


def _runtime():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(users.insert().values(
            id="u1", email="u1@example.com", password_hash="x", created_at=now,
        ))
        connection.execute(tenants.insert().values(id="t1", name="T1", created_at=now))
        connection.execute(memberships.insert().values(
            tenant_id="t1", user_id="u1", role="owner",
        ))
    return engine, PrincipalContext("u1", "t1", "owner")


def test_real_rag_processor_persists_retrieval_evidence_and_cited_report():
    engine, principal = _runtime()
    durable, artifacts = PostgresDurableRepository(engine), PostgresResearchArtifacts(engine)
    retriever = FakeAuthorizedRetriever()
    plan = {
        "execution_profile": "real_rag_local",
        "steps": [{"id": "retrieve_documents", "input": {"question": "现金流", "top_k": 5}}],
    }
    created = durable.create_run(
        principal, company="腾讯控股", question="分析腾讯现金流",
        idempotency_key="real-rag-processor", plan=plan, owner_id="api",
        enqueue_kind="real_rag_local_research",
    )
    processor = FormalRealRagProcessor(
        durable, artifacts, retriever,
        embedding_profile_id="emb-test", index_version="formal-fixture-v1",
    )
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers={"real_rag_local_research": processor}, owner_id="worker:test",
    )
    executor(created.job_id)
    assert durable.get_run(principal, created.run_id)["status"] == "completed"
    assert retriever.calls == 1
    report = artifacts.get_report(principal, created.run_id)
    assert report["report"]["execution_profile"] == "real_rag_local"
    assert report["report"]["fixture"] is True
    assert "腾讯经营活动现金流持续改善。 [1]" in report["markdown"]
    with engine.connect() as connection:
        assert connection.scalar(select(reports_pg.c.run_id)) == created.run_id


def test_real_rag_processor_rejects_low_authority_before_persistence():
    engine, principal = _runtime()
    durable, artifacts = PostgresDurableRepository(engine), PostgresResearchArtifacts(engine)
    retriever = FakeAuthorizedRetriever()
    original = retriever.search
    def low(*args, **kwargs):
        response = original(*args, **kwargs)
        response.results[0].authority_tier = 1
        return response
    retriever.search = low
    created = durable.create_run(
        principal, company="腾讯控股", question="分析腾讯现金流", idempotency_key="low-authority",
        plan={"execution_profile": "real_rag_local", "steps": [{
            "id": "retrieve_documents", "input": {"question": "现金流", "top_k": 5},
        }]}, owner_id="api", enqueue_kind="real_rag_local_research", max_attempts=1,
    )
    processor = FormalRealRagProcessor(
        durable, artifacts, retriever,
        embedding_profile_id="emb-test", index_version="formal-fixture-v1",
    )
    executor = PersistedJobExecutor(
        resolver=WorkerJobContextResolver(engine), ledger=JobLedger(engine), durable=durable,
        handlers={"real_rag_local_research": processor}, owner_id="worker:test",
    )
    try:
        executor(created.job_id)
    except RuntimeError as exc:
        assert "authority" in str(exc)
    else:
        raise AssertionError("low-authority evidence was accepted")
    assert durable.get_run(principal, created.run_id)["status"] == "failed"
