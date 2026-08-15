from __future__ import annotations

from sqlalchemy import Engine, and_, or_, select

from backend.auth.models import PrincipalContext
from backend.db.metadata import retrieval_chunks
from backend.db.rag_catalog import chunk_content_hash
from backend.db.session import principal_transaction
from backend.milvus_retrieval import MilvusHybridRetriever
from backend.retrieval import RetrievalQuery, RetrievalResponse


class AuthorizationSetTooLarge(RuntimeError):
    pass


class AuthorizedChunkCatalog:
    def __init__(self, engine: Engine, *, max_ids: int = 2000):
        self.engine, self.max_ids = engine, max_ids

    def authorizations(self, principal: PrincipalContext, request: RetrievalQuery) -> dict[str, tuple[str, int]]:
        with principal_transaction(self.engine, principal) as connection:
            rows = list(connection.execute(select(
                retrieval_chunks.c.chunk_id, retrieval_chunks.c.content_hash,
                retrieval_chunks.c.authority_tier,
            ).where(and_(
                or_(
                    retrieval_chunks.c.access_scope == "public",
                    retrieval_chunks.c.tenant_id == principal.tenant_id,
                ),
                retrieval_chunks.c.embedding_profile_id == request.embedding_profile_id,
                retrieval_chunks.c.index_version == request.index_version,
                retrieval_chunks.c.content_hash.is_not(None),
            )).order_by(retrieval_chunks.c.chunk_id).limit(self.max_ids + 1)))
        if len(rows) > self.max_ids:
            raise AuthorizationSetTooLarge("authorized retrieval set exceeds expression limit")
        return {str(row.chunk_id): (str(row.content_hash), int(row.authority_tier)) for row in rows}

    def allowed_ids(self, principal: PrincipalContext, request: RetrievalQuery) -> list[str]:
        return list(self.authorizations(principal, request))


class AuthorizedMilvusRetriever:
    def __init__(self, catalog: AuthorizedChunkCatalog, retriever: MilvusHybridRetriever):
        self.catalog, self.retriever = catalog, retriever

    def search(self, principal: PrincipalContext, request: RetrievalQuery) -> RetrievalResponse:
        authorizations = self.catalog.authorizations(principal, request)
        if not authorizations:
            return RetrievalResponse(backend="milvus", mode="hybrid", results=[])
        response = self.retriever.search_authorized(
            request, allowed_chunk_ids=list(authorizations),
        )
        for result in response.results:
            expected = authorizations.get(result.chunk_id)
            if expected is None:
                raise PermissionError("Milvus returned a chunk outside the authorization set")
            if chunk_content_hash(result) != expected[0] or result.authority_tier != expected[1]:
                raise PermissionError("Milvus result identity does not match PostgreSQL authorization")
            for field in ("company", "symbol", "market", "period"):
                required = getattr(request.filters, field)
                if required is not None and getattr(result, field) != required:
                    raise PermissionError("Milvus result does not match the requested filters")
            if (
                request.filters.document_types
                and result.source_type not in request.filters.document_types
            ):
                raise PermissionError("Milvus result does not match the requested filters")
        return response
