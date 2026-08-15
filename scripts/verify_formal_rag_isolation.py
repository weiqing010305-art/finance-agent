from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine

from backend.auth.models import PrincipalContext
from backend.authorized_retrieval import AuthorizedChunkCatalog, AuthorizedMilvusRetriever
from backend.embeddings import BgeLargeZhEmbeddingProvider
from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever
from backend.retrieval import RetrievalFilters, RetrievalQuery


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify PostgreSQL-authorized cross-tenant Milvus filtering")
    parser.add_argument("--tenant-a", required=True)
    parser.add_argument("--tenant-b", required=True)
    parser.add_argument("--user-a", required=True)
    args = parser.parse_args()
    database_url = os.environ["DATABASE_URL"]
    embeddings = BgeLargeZhEmbeddingProvider(device=os.getenv("BGE_DEVICE") or "cpu")
    retriever = AuthorizedMilvusRetriever(
        AuthorizedChunkCatalog(create_engine(database_url, pool_pre_ping=True)),
        MilvusHybridRetriever(MilvusConfig(
            uri=os.getenv("MILVUS_URI", "http://milvus:19530"), token=None,
            collection=os.getenv("MILVUS_COLLECTION", "finance_agent_chunks_v1"),
        ), embeddings),
    )
    query = RetrievalQuery(
        query="租户隔离测试记录", top_k=10, candidate_k=20,
        filters=RetrievalFilters(company="腾讯"),
        embedding_profile_id=embeddings.profile.profile_id,
        index_version=os.getenv("RAG_INDEX_VERSION", "formal_fixture_v1"),
    )
    own = retriever.search(PrincipalContext(args.user_a, args.tenant_a, "owner"), query)
    other = retriever.search(PrincipalContext("isolation-other", args.tenant_b, "owner"), query)
    own_private = [row.chunk_id for row in own.results if "TENANT_A_PRIVATE_MARKER" in row.text]
    leaked = [row.chunk_id for row in other.results if "TENANT_A_PRIVATE_MARKER" in row.text]
    if not own_private or leaked:
        raise RuntimeError(f"tenant isolation failed: own={own_private}, leaked={leaked}")
    print(
        f"formal_rag_isolation_passed own_private={len(own_private)} "
        f"other_results={len(other.results)} leaked=0"
    )


if __name__ == "__main__":
    main()
