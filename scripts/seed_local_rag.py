from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine

from backend.auth.models import PrincipalContext
from backend.db.rag_catalog import PostgresRagCatalog
from backend.embeddings import BgeLargeZhEmbeddingProvider
from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever
from backend.retrieval import IndexedChunk


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "evals" / "formal-rag-fixture.json"


def _load_fixture(path: Path, embeddings: BgeLargeZhEmbeddingProvider) -> tuple[str, list[IndexedChunk]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("fixture") is not True:
        raise ValueError("seed input must be explicitly labelled as fixture data")
    rows = payload.get("documents") or []
    batch = embeddings.embed_documents([str(row["text"]) for row in rows])
    chunks = [
        IndexedChunk(
            **row, embedding=vector, embedding_profile_id=batch.profile_id,
            index_version=str(payload["index_version"]),
        )
        for row, vector in zip(rows, batch.vectors, strict=True)
    ]
    return str(payload["index_version"]), chunks


def seed(
    *, database_url: str, milvus_uri: str, collection: str, tenant_id: str,
    user_id: str, fixture: Path, device: str | None,
) -> dict:
    principal = PrincipalContext(user_id=user_id, tenant_id=tenant_id, role="owner")
    embeddings = BgeLargeZhEmbeddingProvider(device=device)
    index_version, chunks = _load_fixture(fixture, embeddings)
    retriever = MilvusHybridRetriever(
        MilvusConfig(uri=milvus_uri, token=None, collection=collection), embeddings,
    )
    # PostgreSQL is the authorization source. Registering first means a failed
    # Milvus write is invisible/empty and safely retryable; a conflicting ID can
    # never overwrite already-authorized Milvus bytes.
    PostgresRagCatalog(create_engine(database_url, pool_pre_ping=True)).register(principal, chunks)
    retriever.upsert(chunks)
    return {
        "fixture": True, "tenant_id": tenant_id, "collection": collection,
        "index_version": index_version, "embedding_profile_id": embeddings.profile.profile_id,
        "chunks": len(chunks),
    }


def delete_fixture(
    *, database_url: str, milvus_uri: str, collection: str, tenant_id: str,
    user_id: str, fixture: Path,
) -> dict:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    if payload.get("fixture") is not True:
        raise ValueError("delete input must be explicitly labelled as fixture data")
    principal = PrincipalContext(user_id=user_id, tenant_id=tenant_id, role="owner")
    catalog = PostgresRagCatalog(create_engine(database_url, pool_pre_ping=True))
    retriever = MilvusHybridRetriever(
        MilvusConfig(uri=milvus_uri, token=None, collection=collection),
        BgeLargeZhEmbeddingProvider(),
    )
    deleted: list[str] = []
    for version_id in sorted({str(row["document_version_id"]) for row in payload["documents"]}):
        deleted.extend(catalog.unregister_version(principal, version_id))
        retriever.delete_version(version_id)
    return {"fixture": True, "tenant_id": tenant_id, "deleted_chunk_ids": sorted(deleted)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotently seed the labelled local formal-RAG fixture")
    parser.add_argument("action", choices=("seed", "delete"))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.getenv("MILVUS_URI", "http://milvus:19530"))
    parser.add_argument("--collection", default=os.getenv("MILVUS_COLLECTION", "finance_agent_chunks_v1"))
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--device", default=os.getenv("BGE_DEVICE") or None)
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    common = dict(
        database_url=args.database_url, milvus_uri=args.milvus_uri,
        collection=args.collection, tenant_id=args.tenant_id, user_id=args.user_id,
        fixture=args.fixture,
    )
    result = seed(**common, device=args.device) if args.action == "seed" else delete_fixture(**common)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
