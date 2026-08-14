from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest


pytestmark = pytest.mark.milvus


def test_real_milvus_collection_lifecycle_and_hybrid_search():
    uri = os.getenv("MILVUS_TEST_URI")
    if not uri:
        pytest.skip("MILVUS_TEST_URI is not configured; real Milvus metrics were not executed")
    pymilvus = pytest.importorskip("pymilvus")
    from backend.embeddings import EmbeddingBatch, EmbeddingProfile
    from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever
    from backend.retrieval import IndexedChunk, RetrievalFilters, RetrievalQuery

    class FixedEmbeddings:
        profile = EmbeddingProfile()
        def embed_queries(self, texts):
            return EmbeddingBatch(
                profile_id=self.profile.profile_id,
                vectors=[[1.0] + [0.0] * 1023 for _ in texts],
            )
        embed_documents = embed_queries

    collection = "finance_agent_it_" + uuid4().hex
    client = pymilvus.MilvusClient(
        uri=uri, token=os.getenv("MILVUS_TEST_TOKEN") or None
    )
    retriever = MilvusHybridRetriever(
        MilvusConfig(uri=uri, token=os.getenv("MILVUS_TEST_TOKEN") or None, collection=collection),
        FixedEmbeddings(), client=client,
    )
    try:
        retriever.upsert([IndexedChunk(
            chunk_id="c1", document_id="d1", document_version_id="v1",
            text="腾讯经营现金流持续改善", title="年报",
            source_uri="https://example.com/report", publisher="交易所",
            source_type="filing", access_scope="public",
            embedding=[1.0] + [0.0] * 1023,
            embedding_profile_id=FixedEmbeddings.profile.profile_id,
            index_version="integration-v1", company="腾讯", authority_tier=5,
        )])
        client.flush(collection_name=collection)
        response = retriever.search(RetrievalQuery(
            query="现金流", top_k=1, candidate_k=5,
            filters=RetrievalFilters(company="腾讯"),
            embedding_profile_id=FixedEmbeddings.profile.profile_id,
            index_version="integration-v1",
        ))
        assert response.backend == "milvus"
        assert response.results[0].chunk_id == "c1"
    finally:
        if client.has_collection(collection_name=collection):
            client.drop_collection(collection_name=collection)


def test_real_bge_milvus_quality_gate():
    uri = os.getenv("MILVUS_TEST_URI")
    if not uri or os.getenv("RUN_REAL_BGE_TEST") != "1":
        pytest.skip("real BGE gate requires MILVUS_TEST_URI and RUN_REAL_BGE_TEST=1")
    pytest.importorskip("sentence_transformers")
    from scripts.verify_real_rag import run_gate

    result = run_gate(
        uri=uri,
        token=os.getenv("MILVUS_TEST_TOKEN") or None,
        cases_path=Path("evals/real-rag-cases.json"),
    )
    assert result["passed"] is True
    assert result["metrics"]["recall_at_3"] == 1.0
    assert result["metrics"]["mrr_at_3"] >= 0.75
    assert result["filter_checks"] == {
        "company": True, "access_scope": True,
        "profile": True, "index_version": True,
    }
    assert result["cleanup_verified"] is True
