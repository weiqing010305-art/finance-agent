from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.metadata import metadata, retrieval_chunks, tenants
from backend.db.rag_catalog import PostgresRagCatalog, RagCatalogConflict, chunk_content_hash
from backend.retrieval import IndexedChunk


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(tenants.insert().values(id="tenant-a", name="A", created_at=datetime.now(timezone.utc)))
    return engine


def _chunk(**changes):
    values = dict(
        chunk_id="chunk-1", document_id="doc-1", document_version_id="version-1",
        text="本地演示证据", title="fixture", source_uri="https://fixture.invalid/1",
        publisher="FinScope fixture", source_type="local_fixture", access_scope="public",
        embedding=[1.0, 0.0], embedding_profile_id="emb-1", index_version="v1",
        company="腾讯", authority_tier=2,
    )
    values.update(changes)
    return IndexedChunk(**values)


def test_catalog_registration_is_idempotent_and_persists_content_identity():
    engine = _engine(); catalog = PostgresRagCatalog(engine)
    principal = PrincipalContext("user-a", "tenant-a", "owner")
    assert catalog.register(principal, [_chunk()]) == 1
    assert catalog.register(principal, [_chunk()]) == 1
    with engine.connect() as connection:
        row = connection.execute(select(retrieval_chunks)).mappings().one()
    assert row["content_hash"] == chunk_content_hash(_chunk())
    assert row["authority_tier"] == 2


def test_catalog_rejects_same_id_with_mutated_content_or_authority():
    engine = _engine(); catalog = PostgresRagCatalog(engine)
    principal = PrincipalContext("user-a", "tenant-a", "owner")
    catalog.register(principal, [_chunk()])
    with pytest.raises(RagCatalogConflict, match="identity"):
        catalog.register(principal, [_chunk(text="mutated", authority_tier=5)])


def test_catalog_can_adopt_matching_pre_identity_migration_row():
    engine = _engine(); principal = PrincipalContext("user-a", "tenant-a", "owner")
    with engine.begin() as connection:
        connection.execute(retrieval_chunks.insert().values(
            chunk_id="chunk-1", tenant_id="tenant-a", document_id="doc-1",
            document_version_id="version-1", access_scope="public",
            embedding_profile_id="emb-1", index_version="v1", content_hash=None,
            authority_tier=0, created_at=datetime.now(timezone.utc),
        ))
    PostgresRagCatalog(engine).register(principal, [_chunk()])
    with engine.connect() as connection:
        row = connection.execute(select(retrieval_chunks)).mappings().one()
    assert row["content_hash"] == chunk_content_hash(_chunk()) and row["authority_tier"] == 2
