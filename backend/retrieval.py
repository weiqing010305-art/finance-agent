from __future__ import annotations

import math
import re
from collections import Counter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.embeddings import EmbeddingProvider


class RetrievalFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str | None = Field(default=None, max_length=120)
    symbol: str | None = Field(default=None, max_length=32)
    market: str | None = Field(default=None, max_length=16)
    period: str | None = Field(default=None, max_length=32)
    document_types: list[str] = Field(default_factory=list, max_length=20)
    access_scope: str = Field(default="public", min_length=1, max_length=128)


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=100)
    candidate_k: int = Field(default=40, ge=1, le=500)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    embedding_profile_id: str = Field(min_length=1, max_length=128)
    index_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def candidate_limit(self):
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k cannot be less than top_k")
        return self


class IndexedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str
    document_id: str
    document_version_id: str
    text: str = Field(min_length=1)
    title: str
    source_uri: str
    publisher: str
    source_type: str
    access_scope: str = "public"
    embedding: list[float]
    embedding_profile_id: str
    index_version: str
    company: str | None = None
    symbol: str | None = None
    market: str | None = None
    period: str | None = None
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    authority_tier: int = Field(default=0, ge=0, le=5)
    published_at: str | None = None


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str
    document_id: str
    document_version_id: str
    text: str
    title: str
    source_uri: str
    publisher: str
    source_type: str
    access_scope: str
    dense_score: float = 0
    sparse_score: float = 0
    fused_score: float
    dense_rank: int | None = Field(default=None, ge=1)
    sparse_rank: int | None = Field(default=None, ge=1)
    rank: int = Field(ge=1)
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    authority_tier: int = Field(ge=0, le=5)
    published_at: str | None = None
    embedding_profile_id: str
    index_version: str
    company: str | None = None
    symbol: str | None = None
    market: str | None = None
    period: str | None = None


class RetrievalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["milvus", "in_memory_test"]
    mode: Literal["hybrid", "dense_only", "bm25_only"]
    fusion: Literal["rrf"] = "rrf"
    results: list[RetrievalResult]
    degraded: bool = False
    degraded_reason: str | None = None


class HybridRetriever(Protocol):
    backend_name: str

    def upsert(self, chunks: list[IndexedChunk]) -> None: ...
    def delete_version(self, document_version_id: str) -> None: ...
    def search(self, request: RetrievalQuery) -> RetrievalResponse: ...
    def health(self) -> dict: ...


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9_.%-]+", lowered)
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    chinese: list[str] = []
    for run in chinese_runs:
        chinese.extend(run if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
    return latin + chinese


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("query and document embedding dimensions differ")
    return sum(a * b for a, b in zip(left, right, strict=True))


class InMemoryHybridRetriever:
    """Deterministic contract oracle. Never selected by production configuration."""

    backend_name = "in_memory_test"

    def __init__(self, embeddings: EmbeddingProvider, *, rrf_k: int = 60):
        self.embeddings = embeddings
        self.rrf_k = rrf_k
        self._chunks: dict[str, IndexedChunk] = {}

    def upsert(self, chunks: list[IndexedChunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk

    def delete_version(self, document_version_id: str) -> None:
        self._chunks = {
            key: value for key, value in self._chunks.items()
            if value.document_version_id != document_version_id
        }

    def health(self) -> dict:
        return {"ok": True, "backend": self.backend_name, "chunks": len(self._chunks)}

    @staticmethod
    def _matches(chunk: IndexedChunk, filters: RetrievalFilters) -> bool:
        for field in ("company", "symbol", "market", "period", "access_scope"):
            expected = getattr(filters, field)
            if expected is not None and getattr(chunk, field) != expected:
                return False
        if filters.document_types and chunk.source_type not in filters.document_types:
            return False
        return True

    def search(self, request: RetrievalQuery) -> RetrievalResponse:
        candidates = [
            chunk for chunk in self._chunks.values()
            if self._matches(chunk, request.filters)
            and chunk.embedding_profile_id == request.embedding_profile_id
            and chunk.index_version == request.index_version
        ]
        if not candidates:
            return RetrievalResponse(backend="in_memory_test", mode="hybrid", results=[])
        query_vector = self.embeddings.embed_queries([request.query]).vectors[0]
        dense_scores = {chunk.chunk_id: _cosine(query_vector, chunk.embedding) for chunk in candidates}

        query_terms = _tokens(request.query)
        term_docs = Counter(term for chunk in candidates for term in set(_tokens(chunk.text)))
        avg_len = sum(max(1, len(_tokens(chunk.text))) for chunk in candidates) / len(candidates)
        sparse_scores: dict[str, float] = {}
        for chunk in candidates:
            terms = _tokens(chunk.text)
            counts = Counter(terms)
            score = 0.0
            for term in query_terms:
                df = term_docs[term]
                idf = math.log(1 + (len(candidates) - df + 0.5) / (df + 0.5))
                tf = counts[term]
                score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(terms) / avg_len)) if tf else 0
            sparse_scores[chunk.chunk_id] = score

        dense_order = sorted(candidates, key=lambda c: (-dense_scores[c.chunk_id], c.chunk_id))[:request.candidate_k]
        sparse_order = sorted(candidates, key=lambda c: (-sparse_scores[c.chunk_id], c.chunk_id))[:request.candidate_k]
        dense_rank = {chunk.chunk_id: index + 1 for index, chunk in enumerate(dense_order)}
        sparse_rank = {chunk.chunk_id: index + 1 for index, chunk in enumerate(sparse_order)}
        fused = {
            chunk.chunk_id: 1 / (self.rrf_k + dense_rank[chunk.chunk_id]) + 1 / (self.rrf_k + sparse_rank[chunk.chunk_id])
            for chunk in candidates
        }
        ordered = sorted(
            candidates,
            key=lambda c: (-fused[c.chunk_id], -c.authority_tier, c.chunk_id),
        )
        # Diversity: one result per chunk, at most three per document in the first page.
        selected: list[IndexedChunk] = []
        per_document: Counter[str] = Counter()
        for chunk in ordered:
            if per_document[chunk.document_id] >= 3:
                continue
            selected.append(chunk)
            per_document[chunk.document_id] += 1
            if len(selected) >= request.top_k:
                break
        results = [
            RetrievalResult(
                **chunk.model_dump(exclude={"embedding"}),
                dense_score=dense_scores[chunk.chunk_id],
                sparse_score=sparse_scores[chunk.chunk_id],
                fused_score=fused[chunk.chunk_id],
                dense_rank=dense_rank[chunk.chunk_id],
                sparse_rank=sparse_rank[chunk.chunk_id],
                rank=index + 1,
            )
            for index, chunk in enumerate(selected)
        ]
        return RetrievalResponse(backend="in_memory_test", mode="hybrid", results=results)
