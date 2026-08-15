from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.authorized_retrieval import AuthorizedChunkCatalog, AuthorizedMilvusRetriever
from backend.db.rag_catalog import chunk_content_hash
from backend.db.metadata import metadata, retrieval_chunks, tenants, users
from backend.retrieval import RetrievalFilters, RetrievalQuery, RetrievalResponse, RetrievalResult
from backend.milvus_retrieval import build_filter


class SpyRetriever:
    def __init__(self): self.allowed = None
    def search_authorized(self, request, *, allowed_chunk_ids):
        self.allowed = allowed_chunk_ids
        return RetrievalResponse(backend="milvus", mode="hybrid", results=[])


def _setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine); now = datetime.now(timezone.utc)
    with engine.begin() as c:
        for tenant in ("a", "b"):
            c.execute(tenants.insert().values(id=tenant, name=tenant, created_at=now))
        for chunk, tenant, scope in (("a-private", "a", "private"), ("b-private", "b", "private"), ("b-public", "b", "public")):
            c.execute(retrieval_chunks.insert().values(
                chunk_id=chunk, tenant_id=tenant, document_id=chunk, document_version_id=chunk,
                access_scope=scope, embedding_profile_id="bge", index_version="v1", created_at=now,
                content_hash=f"hash-{chunk}", authority_tier=2,
            ))
    return engine


def _query():
    return RetrievalQuery(
        query="cash flow", filters=RetrievalFilters(access_scope="private"),
        embedding_profile_id="bge", index_version="v1",
    )


def test_postgres_catalog_allows_own_private_and_shared_public_only():
    engine = _setup(); spy = SpyRetriever()
    AuthorizedMilvusRetriever(AuthorizedChunkCatalog(engine), spy).search(
        PrincipalContext("u", "a", "viewer"), _query()
    )
    assert spy.allowed == ["a-private", "b-public"]
    assert "b-private" not in spy.allowed


def test_empty_authorization_never_calls_milvus():
    engine = _setup(); spy = SpyRetriever()
    query = _query().model_copy(update={"embedding_profile_id": "missing"})
    response = AuthorizedMilvusRetriever(AuthorizedChunkCatalog(engine), spy).search(
        PrincipalContext("u", "a", "viewer"), query
    )
    assert response.results == [] and spy.allowed is None


def test_authorized_filter_uses_escaped_ids_not_caller_scope():
    expression = build_filter(_query(), allowed_chunk_ids=['a-private', 'x" or true'])
    assert 'chunk_id in ["a-private","x\\" or true"]' in expression
    assert "access_scope ==" not in expression


def test_milvus_result_must_match_postgres_owned_content_identity():
    engine = _setup()

    class MutatedRetriever(SpyRetriever):
        def search_authorized(self, request, *, allowed_chunk_ids):
            self.allowed = allowed_chunk_ids
            return RetrievalResponse(backend="milvus", mode="hybrid", results=[RetrievalResult(
                chunk_id="a-private", document_id="a-private",
                document_version_id="a-private", text="mutated", title="title",
                source_uri="https://fixture.invalid/a", publisher="fixture",
                source_type="fixture", access_scope="private", fused_score=1, rank=1,
                authority_tier=5, embedding_profile_id="bge", index_version="v1",
            )])

    import pytest
    with pytest.raises(PermissionError, match="identity"):
        AuthorizedMilvusRetriever(AuthorizedChunkCatalog(engine), MutatedRetriever()).search(
            PrincipalContext("u", "a", "viewer"), _query()
        )


def test_authorized_but_wrong_company_result_is_rejected_after_milvus():
    result = RetrievalResult(
        chunk_id="moutai", document_id="moutai", document_version_id="moutai-v1",
        text="贵州茅台现金流", title="fixture", source_uri="https://fixture.invalid/moutai",
        publisher="fixture", source_type="local_fixture", access_scope="public",
        fused_score=1, rank=1, authority_tier=2, embedding_profile_id="bge",
        index_version="v1", company="贵州茅台",
    )

    class Catalog:
        def authorizations(self, principal, request):
            return {"moutai": (chunk_content_hash(result), 2)}

    class Retriever:
        def search_authorized(self, request, *, allowed_chunk_ids):
            return RetrievalResponse(backend="milvus", mode="hybrid", results=[result])

    query = _query().model_copy(update={
        "filters": RetrievalFilters(company="腾讯", access_scope="private"),
    })
    import pytest
    with pytest.raises(PermissionError, match="requested filters"):
        AuthorizedMilvusRetriever(Catalog(), Retriever()).search(
            PrincipalContext("u", "a", "viewer"), query,
        )
