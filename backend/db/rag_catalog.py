from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import Engine, and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.auth.models import PrincipalContext
from backend.db.metadata import retrieval_chunks
from backend.db.session import principal_transaction
from backend.retrieval import IndexedChunk, RetrievalResult


class RagCatalogConflict(RuntimeError):
    pass


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chunk_identity(value: IndexedChunk | RetrievalResult) -> dict:
    optional_text = lambda item: str(item) if item not in (None, "") else None
    return {
        "chunk_id": value.chunk_id,
        "document_id": value.document_id,
        "document_version_id": value.document_version_id,
        "text": value.text,
        "title": value.title,
        "source_uri": value.source_uri,
        "publisher": value.publisher,
        "source_type": value.source_type,
        "access_scope": value.access_scope,
        "company": optional_text(value.company),
        "symbol": optional_text(value.symbol),
        "market": optional_text(value.market),
        "period": optional_text(value.period),
        "page": value.page,
        "section": optional_text(value.section),
        "authority_tier": value.authority_tier,
        "published_at": optional_text(value.published_at),
        "embedding_profile_id": value.embedding_profile_id,
        "index_version": value.index_version,
    }


def chunk_content_hash(value: IndexedChunk | RetrievalResult) -> str:
    return hashlib.sha256(_canonical(chunk_identity(value)).encode()).hexdigest()


class PostgresRagCatalog:
    def __init__(self, engine: Engine):
        self.engine = engine

    def register(self, principal: PrincipalContext, chunks: list[IndexedChunk]) -> int:
        now = datetime.now(timezone.utc)
        with principal_transaction(self.engine, principal) as connection:
            for chunk in chunks:
                values = {
                    "chunk_id": chunk.chunk_id,
                    "tenant_id": principal.tenant_id,
                    "document_id": chunk.document_id,
                    "document_version_id": chunk.document_version_id,
                    "access_scope": chunk.access_scope,
                    "embedding_profile_id": chunk.embedding_profile_id,
                    "index_version": chunk.index_version,
                    "content_hash": chunk_content_hash(chunk),
                    "authority_tier": chunk.authority_tier,
                    "created_at": now,
                }
                statement = (
                    postgresql_insert(retrieval_chunks) if connection.dialect.name == "postgresql"
                    else sqlite_insert(retrieval_chunks)
                ).values(**values).on_conflict_do_nothing(
                    index_elements=[retrieval_chunks.c.chunk_id]
                )
                connection.execute(statement)
                persisted = connection.execute(select(retrieval_chunks).where(and_(
                    retrieval_chunks.c.chunk_id == chunk.chunk_id,
                    retrieval_chunks.c.tenant_id == principal.tenant_id,
                ))).mappings().one_or_none()
                base_keys = (
                    "tenant_id", "document_id", "document_version_id", "access_scope",
                    "embedding_profile_id", "index_version",
                )
                if (
                    persisted is not None
                    and persisted["content_hash"] is None
                    and all(persisted[key] == values[key] for key in base_keys)
                ):
                    # Rows created before schema 0012 are deliberately unauthorized.
                    # An explicit fixture re-index may adopt them once their old
                    # authorization identity matches; Milvus is written afterwards.
                    connection.execute(update(retrieval_chunks).where(and_(
                        retrieval_chunks.c.chunk_id == chunk.chunk_id,
                        retrieval_chunks.c.tenant_id == principal.tenant_id,
                        retrieval_chunks.c.content_hash.is_(None),
                    )).values(
                        content_hash=values["content_hash"], authority_tier=values["authority_tier"],
                    ))
                    persisted = connection.execute(select(retrieval_chunks).where(and_(
                        retrieval_chunks.c.chunk_id == chunk.chunk_id,
                        retrieval_chunks.c.tenant_id == principal.tenant_id,
                    ))).mappings().one_or_none()
                expected = {key: values[key] for key in (
                    "tenant_id", "document_id", "document_version_id", "access_scope",
                    "embedding_profile_id", "index_version", "content_hash", "authority_tier",
                )}
                if persisted is None or any(persisted[key] != value for key, value in expected.items()):
                    raise RagCatalogConflict("retrieval chunk identity conflict")
        return len(chunks)

    def unregister_version(
        self, principal: PrincipalContext, document_version_id: str,
    ) -> list[str]:
        with principal_transaction(self.engine, principal) as connection:
            ids = list(connection.scalars(select(retrieval_chunks.c.chunk_id).where(and_(
                retrieval_chunks.c.tenant_id == principal.tenant_id,
                retrieval_chunks.c.document_version_id == document_version_id,
            ))))
            connection.execute(delete(retrieval_chunks).where(and_(
                retrieval_chunks.c.tenant_id == principal.tenant_id,
                retrieval_chunks.c.document_version_id == document_version_id,
            )))
        return ids
