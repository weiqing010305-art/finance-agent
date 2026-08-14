from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.authorized_retrieval import AuthorizedChunkCatalog, AuthorizedMilvusRetriever
from backend.db.metadata import metadata, retrieval_chunks, tenants, users
from backend.retrieval import RetrievalFilters, RetrievalQuery, RetrievalResponse
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
