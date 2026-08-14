from __future__ import annotations

import hashlib
import math

from backend.embeddings import EmbeddingBatch, EmbeddingProfile
from backend.retrieval import (
    IndexedChunk, InMemoryHybridRetriever, RetrievalFilters, RetrievalQuery,
)


class HashEmbeddings:
    profile = EmbeddingProfile()

    def _embed(self, texts):
        vectors = []
        for text in texts:
            values = [0.0] * 1024
            for token in text:
                values[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 1024] += 1
            norm = math.sqrt(sum(v * v for v in values)) or 1
            vectors.append([v / norm for v in values])
        return EmbeddingBatch(profile_id=self.profile.profile_id, vectors=vectors)

    embed_queries = _embed
    embed_documents = _embed


def _chunk(embedding, chunk_id, text, *, doc="d1", company="腾讯", authority=3):
    return IndexedChunk(
        chunk_id=chunk_id, document_id=doc, document_version_id=doc + "v1",
        text=text, title="报告", source_uri=f"https://example.com/{doc}",
        publisher="交易所", source_type="filing", access_scope="public",
        embedding=embedding.embed_documents([text]).vectors[0],
        embedding_profile_id=embedding.profile.profile_id, index_version="idx-v1",
        company=company, symbol="0700.HK", market="HK", authority_tier=authority,
    )


def test_in_memory_hybrid_rrf_filters_and_versions():
    embedding = HashEmbeddings()
    retriever = InMemoryHybridRetriever(embedding)
    retriever.upsert([
        _chunk(embedding, "c1", "腾讯经营现金流持续改善", authority=5),
        _chunk(embedding, "c2", "腾讯盈利质量和现金转换率", doc="d2", authority=3),
        _chunk(embedding, "c3", "贵州茅台现金流", doc="d3", company="茅台"),
    ])
    response = retriever.search(RetrievalQuery(
        query="现金流质量", top_k=2, candidate_k=3,
        filters=RetrievalFilters(company="腾讯", access_scope="public"),
        embedding_profile_id=embedding.profile.profile_id, index_version="idx-v1",
    ))
    assert response.backend == "in_memory_test"
    assert [hit.chunk_id for hit in response.results] == ["c1", "c2"]
    assert all(hit.dense_rank and hit.sparse_rank for hit in response.results)
    assert all(hit.embedding_profile_id == embedding.profile.profile_id for hit in response.results)


def test_upsert_delete_stable_ties_and_document_diversity():
    embedding = HashEmbeddings()
    retriever = InMemoryHybridRetriever(embedding)
    retriever.upsert([
        _chunk(embedding, f"c{i}", "相同内容", doc="same") for i in range(5)
    ] + [_chunk(embedding, "other", "相同内容", doc="other", authority=5)])
    request = RetrievalQuery(
        query="相同内容", top_k=5, candidate_k=6,
        embedding_profile_id=embedding.profile.profile_id, index_version="idx-v1",
    )
    first = retriever.search(request)
    second = retriever.search(request)
    assert [hit.chunk_id for hit in first.results] == [hit.chunk_id for hit in second.results]
    assert sum(hit.document_id == "same" for hit in first.results) <= 3
    retriever.delete_version("samev1")
    assert [hit.chunk_id for hit in retriever.search(request).results] == ["other"]
