from __future__ import annotations

from backend.database import Repository
from backend.embeddings import EmbeddingProfile
from backend.milvus_retrieval import MilvusUnavailable
from backend.retrieval import HybridRetriever, RetrievalFilters, RetrievalQuery
from backend.tool_registry import HybridRetrievalInput, ToolInvocationContext


class RetrieveDocumentsTool:
    def __init__(
        self, repository: Repository, retriever: HybridRetriever,
        *, embedding_profile: EmbeddingProfile, index_version: str,
    ) -> None:
        self.repository = repository
        self.retriever = retriever
        self.embedding_profile = embedding_profile
        self.index_version = index_version

    async def __call__(
        self, payload: HybridRetrievalInput, context: ToolInvocationContext | None = None,
    ) -> dict:
        if context is None:
            raise ValueError("retrieve_documents requires an invocation context")
        run = self.repository.get_task(context.run_id)
        if run is None:
            raise ValueError("run does not exist")
        for field in ("company", "symbol", "market"):
            requested = getattr(payload, field)
            confirmed = run.get(field)
            if requested and confirmed and requested != confirmed:
                raise ValueError(f"retrieval {field} cannot widen confirmed run scope")
        request = RetrievalQuery(
            query=payload.question or run["question"], top_k=payload.top_k,
            candidate_k=max(payload.top_k, min(200, payload.top_k * 4)),
            filters=RetrievalFilters(
                company=run["company"], symbol=run.get("symbol"), market=run["market"],
                access_scope="public",
            ),
            embedding_profile_id=self.embedding_profile.profile_id,
            index_version=self.index_version,
        )
        try:
            response = self.retriever.search(request)
        except MilvusUnavailable as exc:
            raise RuntimeError("document retrieval is unavailable") from exc
        hits = [
            {
                "chunk_id": hit.chunk_id, "document_id": hit.document_id,
                "text": hit.text, "dense_score": hit.dense_score,
                "sparse_score": hit.sparse_score, "fused_score": hit.fused_score,
                "source_id": hit.document_version_id, "page": hit.page,
            }
            for hit in response.results
        ]
        evidence = [
            {
                "source_id": hit.document_version_id, "title": hit.title,
                "url": hit.source_uri, "publisher": hit.publisher, "page": hit.page,
            }
            for hit in response.results
        ]
        fallback = None if response.mode == "hybrid" else response.mode
        return {
            "status": "ok" if hits else "empty", "data": hits, "evidence": evidence,
            "degraded": response.degraded, "degraded_reason": response.degraded_reason,
            "fallback_used": fallback,
            "retrieval": {
                "backend": "milvus", "mode": response.mode, "fusion": "rrf",
                "sparse": "bm25", "dense_model_version": self.embedding_profile.profile_id,
                "index_version": self.index_version,
            },
        }
