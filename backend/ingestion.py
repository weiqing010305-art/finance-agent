from __future__ import annotations

from backend.database import Repository, utc_now
from backend.embeddings import EmbeddingProvider
from backend.retrieval import HybridRetriever, IndexedChunk


class IngestionService:
    def __init__(self, repository: Repository, embeddings: EmbeddingProvider, retriever: HybridRetriever):
        self.repository = repository
        self.embeddings = embeddings
        self.retriever = retriever

    def index_version(self, version_id: str, *, embedding_profile_id: str, index_version: str) -> int:
        claim_token = self.repository.claim_ingestion_job(
            version_id, embedding_profile_id=embedding_profile_id,
            index_version=index_version,
        )
        if claim_token is None:
            return 0
        try:
            return self._index_claimed(
                version_id, claim_token=claim_token, embedding_profile_id=embedding_profile_id,
                index_version=index_version,
            )
        except Exception as exc:
            self.repository.finish_ingestion_job(
                version_id, claim_token=claim_token, embedding_profile_id=embedding_profile_id,
                index_version=index_version, success=False, error=type(exc).__name__,
            )
            raise

    def _index_claimed(self, version_id: str, *, claim_token: str, embedding_profile_id: str, index_version: str) -> int:
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*,v.document_id,d.title,d.source_uri,d.publisher,d.source_type,
                       d.access_scope,d.company,d.symbol,d.market,v.published_at
                FROM document_chunks c
                JOIN document_versions v ON v.id=c.document_version_id
                JOIN documents d ON d.id=v.document_id
                WHERE c.document_version_id=? ORDER BY c.ordinal
                """,
                (version_id,),
            ).fetchall()
        if not rows:
            raise ValueError("document version has no persisted chunks")
        batch = self.embeddings.embed_documents([row["text"] for row in rows])
        if batch.profile_id != embedding_profile_id:
            raise ValueError("embedding provider returned a different profile")
        vectors = batch.vectors
        chunks = [
            IndexedChunk(
                chunk_id=row["id"], document_id=row["document_id"],
                document_version_id=row["document_version_id"], text=row["text"],
                title=row["title"], source_uri=row["source_uri"], publisher=row["publisher"],
                source_type=row["source_type"], access_scope=row["access_scope"],
                embedding=vector, embedding_profile_id=embedding_profile_id,
                index_version=index_version, company=row["company"], symbol=row["symbol"],
                market=row["market"], page=row["page"], section=row["section"],
                published_at=row["published_at"],
            )
            for row, vector in zip(rows, vectors, strict=True)
        ]
        self.retriever.upsert(chunks)
        self.repository.finish_ingestion_job(
            version_id, claim_token=claim_token, embedding_profile_id=embedding_profile_id,
            index_version=index_version, success=True,
        )
        return len(chunks)
