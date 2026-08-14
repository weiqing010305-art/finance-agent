from __future__ import annotations

from sqlalchemy import Engine, and_, or_, select

from backend.auth.models import PrincipalContext
from backend.db.metadata import retrieval_chunks
from backend.db.session import principal_transaction
from backend.milvus_retrieval import MilvusHybridRetriever
from backend.retrieval import RetrievalQuery, RetrievalResponse


class AuthorizationSetTooLarge(RuntimeError):
    pass


class AuthorizedChunkCatalog:
    def __init__(self, engine: Engine, *, max_ids: int = 2000):
        self.engine, self.max_ids = engine, max_ids

    def allowed_ids(self, principal: PrincipalContext, request: RetrievalQuery) -> list[str]:
        with principal_transaction(self.engine, principal) as connection:
            rows = list(connection.scalars(select(retrieval_chunks.c.chunk_id).where(and_(
                or_(
                    retrieval_chunks.c.access_scope == "public",
                    retrieval_chunks.c.tenant_id == principal.tenant_id,
                ),
                retrieval_chunks.c.embedding_profile_id == request.embedding_profile_id,
                retrieval_chunks.c.index_version == request.index_version,
            )).order_by(retrieval_chunks.c.chunk_id).limit(self.max_ids + 1)))
        if len(rows) > self.max_ids:
            raise AuthorizationSetTooLarge("authorized retrieval set exceeds expression limit")
        return rows


class AuthorizedMilvusRetriever:
    def __init__(self, catalog: AuthorizedChunkCatalog, retriever: MilvusHybridRetriever):
        self.catalog, self.retriever = catalog, retriever

    def search(self, principal: PrincipalContext, request: RetrievalQuery) -> RetrievalResponse:
        allowed = self.catalog.allowed_ids(principal, request)
        if not allowed:
            return RetrievalResponse(backend="milvus", mode="hybrid", results=[])
        return self.retriever.search_authorized(request, allowed_chunk_ids=allowed)
