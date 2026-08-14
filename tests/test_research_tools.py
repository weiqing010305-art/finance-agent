from __future__ import annotations

import asyncio

import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner
from backend.embeddings import EmbeddingProfile
from backend.research_tools import RetrieveDocumentsTool
from backend.retrieval import RetrievalResponse, RetrievalResult
from backend.schemas import ResearchCreate
from backend.tool_registry import HybridRetrievalInput, ToolInvocationContext


class CapturingRetriever:
    backend_name = "milvus"
    def __init__(self, *, degraded=False): self.request = None; self.degraded = degraded
    def search(self, request):
        self.request = request
        return RetrievalResponse(
            backend="milvus", mode="bm25_only" if self.degraded else "hybrid",
            degraded=self.degraded,
            degraded_reason="dense_route_failed" if self.degraded else None,
            results=[RetrievalResult(
                chunk_id="c1", document_id="d1", document_version_id="v1",
                text="经营现金流改善", title="年报", source_uri="https://example.com/a",
                publisher="交易所", source_type="filing", access_scope="public",
                sparse_score=1.2, fused_score=0.9, sparse_rank=1, rank=1,
                page=2, authority_tier=5,
                embedding_profile_id=request.embedding_profile_id,
                index_version=request.index_version,
            )],
        )


def _run(tmp_path):
    repo = Repository(tmp_path / "tools.db"); repo.initialize(); runner = DurableRunner(repo)
    created = runner.create_run(
        ResearchCreate(company="腾讯", symbol="0700.HK", market="HK", question="分析现金流"),
        owner_id="test", idempotency_key="tool-run",
    )
    return repo, created.run


def test_retrieve_documents_enforces_confirmed_entity_and_returns_versions(tmp_path):
    repo, run = _run(tmp_path); retriever = CapturingRetriever(); profile = EmbeddingProfile()
    tool = RetrieveDocumentsTool(
        repo, retriever, embedding_profile=profile, index_version="idx-v1"
    )
    context = ToolInvocationContext(
        run_id=run["id"], plan_version=1, step_id="retrieve_documents", idempotency_key="x"
    )
    result = asyncio.run(tool(HybridRetrievalInput(
        company="腾讯", symbol="0700.HK", market="HK", question="现金流",
        retrieval_mode="hybrid", top_k=5,
    ), context))
    assert retriever.request.filters.company == "腾讯"
    assert result["retrieval"]["dense_model_version"] == profile.profile_id
    assert result["data"][0]["chunk_id"] == "c1"
    with pytest.raises(ValueError, match="cannot widen"):
        asyncio.run(tool(HybridRetrievalInput(
            company="茅台", market="HK", question="现金流", retrieval_mode="hybrid"
        ), context))


def test_degraded_route_is_disclosed(tmp_path):
    repo, run = _run(tmp_path); retriever = CapturingRetriever(degraded=True)
    tool = RetrieveDocumentsTool(
        repo, retriever, embedding_profile=EmbeddingProfile(), index_version="idx-v1"
    )
    result = asyncio.run(tool(HybridRetrievalInput(
        company="腾讯", market="HK", question="现金流", retrieval_mode="hybrid"
    ), ToolInvocationContext(
        run_id=run["id"], plan_version=1, step_id="retrieve_documents", idempotency_key="x"
    )))
    assert result["degraded"] is True
    assert result["fallback_used"] == "bm25_only"
    assert result["degraded_reason"] == "dense_route_failed"
