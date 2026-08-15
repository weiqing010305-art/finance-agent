import os

import pytest
from sqlalchemy import create_engine

from backend.auth.models import PrincipalContext
from backend.authorized_retrieval import AuthorizedChunkCatalog, AuthorizedMilvusRetriever
from backend.embeddings import BgeLargeZhEmbeddingProvider
from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever
from backend.retrieval import RetrievalFilters, RetrievalQuery


@pytest.mark.integration
def test_real_catalog_milvus_fence_excludes_another_tenants_private_fixture():
    database_url = os.getenv("FORMAL_RAG_DATABASE_URL")
    tenant_a = os.getenv("FORMAL_RAG_TENANT_A")
    tenant_b = os.getenv("FORMAL_RAG_TENANT_B")
    if not database_url or not tenant_a or not tenant_b:
        pytest.skip("formal PostgreSQL/Milvus integration environment is not configured")
    embeddings = BgeLargeZhEmbeddingProvider(device=os.getenv("BGE_DEVICE") or "cpu")
    retriever = AuthorizedMilvusRetriever(
        AuthorizedChunkCatalog(create_engine(database_url, pool_pre_ping=True)),
        MilvusHybridRetriever(MilvusConfig(
            uri=os.getenv("MILVUS_TEST_URI", "http://127.0.0.1:19530"), token=None,
            collection=os.getenv("MILVUS_COLLECTION", "finance_agent_chunks_v1"),
        ), embeddings),
    )
    query = RetrievalQuery(
        query="租户隔离测试记录", top_k=10, candidate_k=20,
        filters=RetrievalFilters(company="腾讯"),
        embedding_profile_id=embeddings.profile.profile_id,
        index_version=os.getenv("RAG_INDEX_VERSION", "formal_fixture_v1"),
    )
    own = retriever.search(PrincipalContext("integration-a", tenant_a, "owner"), query)
    other = retriever.search(PrincipalContext("integration-b", tenant_b, "owner"), query)
    assert any("TENANT_A_PRIVATE_MARKER" in row.text for row in own.results)
    assert all("TENANT_A_PRIVATE_MARKER" not in row.text for row in other.results)
