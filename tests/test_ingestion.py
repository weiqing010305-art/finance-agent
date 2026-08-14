from __future__ import annotations

import sqlite3

import pytest

from backend.database import Repository
from backend.documents import ingest_document
from backend.embeddings import EmbeddingBatch, EmbeddingProfile
from backend.ingestion import IngestionService
from backend.schemas import DocumentSource


class FixedEmbeddings:
    profile = EmbeddingProfile()
    def embed_documents(self, texts):
        return EmbeddingBatch(
            profile_id=self.profile.profile_id,
            vectors=[[1.0] + [0.0] * 1023 for _ in texts],
        )
    embed_queries = embed_documents


class CapturingIndex:
    backend_name = "in_memory_test"
    def __init__(self): self.chunks = []
    def upsert(self, chunks): self.chunks.extend(chunks)
    def delete_version(self, version): pass
    def search(self, request): raise NotImplementedError
    def health(self): return {"ok": True}


def test_persisted_job_becomes_indexed_only_after_successful_upsert(tmp_path):
    repo = Repository(tmp_path / "ingestion.db"); repo.initialize()
    profile = FixedEmbeddings.profile
    version, persisted_chunks, _ = ingest_document(
        repo,
        DocumentSource(
            source_uri="https://example.com/filing", source_type="filing",
            title="年报", publisher="交易所", company="腾讯", symbol="0700.HK", market="HK",
        ),
        "# 现金流\n经营现金流持续改善。",
        embedding_profile_id=profile.profile_id, index_version="idx-v1",
    )
    index = CapturingIndex()
    count = IngestionService(repo, FixedEmbeddings(), index).index_version(
        version["id"], embedding_profile_id=profile.profile_id, index_version="idx-v1"
    )
    assert count == len(persisted_chunks) == len(index.chunks)
    assert all(len(chunk.embedding) == 1024 for chunk in index.chunks)
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT status FROM ingestion_jobs").fetchone()[0] == "indexed"


def test_failed_upsert_leaves_job_pending_for_reconciliation(tmp_path):
    repo = Repository(tmp_path / "pending.db"); repo.initialize()
    version, _, _ = ingest_document(
        repo,
        DocumentSource(
            source_uri="https://example.com/a", source_type="filing",
            title="报告", publisher="交易所",
        ),
        "正文内容",
        embedding_profile_id=FixedEmbeddings.profile.profile_id, index_version="idx-v1",
    )
    class Failing(CapturingIndex):
        def upsert(self, chunks): raise RuntimeError("Milvus down")
    try:
        IngestionService(repo, FixedEmbeddings(), Failing()).index_version(
            version["id"], embedding_profile_id=FixedEmbeddings.profile.profile_id,
            index_version="idx-v1",
        )
    except RuntimeError:
        pass
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT status FROM ingestion_jobs").fetchone()[0] == "failed"


def test_embedding_provider_profile_mismatch_fails_before_index_upsert(tmp_path):
    repo = Repository(tmp_path / "profile.db"); repo.initialize()
    version, _, _ = ingest_document(
        repo,
        DocumentSource(source_uri="https://example.com/p", source_type="filing", title="报告", publisher="交易所"),
        "正文内容", embedding_profile_id="expected-profile", index_version="idx-v1",
    )
    index = CapturingIndex()
    with pytest.raises(ValueError, match="different profile"):
        IngestionService(repo, FixedEmbeddings(), index).index_version(
            version["id"], embedding_profile_id="expected-profile", index_version="idx-v1",
        )
    assert index.chunks == []


def test_concurrent_ingestion_has_one_claim_owner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    import time

    repo = Repository(tmp_path / "concurrent-index.db"); repo.initialize()
    version, _, _ = ingest_document(
        repo,
        DocumentSource(source_uri="https://example.com/c", source_type="filing", title="报告", publisher="交易所"),
        "正文内容", embedding_profile_id=FixedEmbeddings.profile.profile_id,
        index_version="idx-v1",
    )
    class SlowIndex(CapturingIndex):
        def __init__(self): super().__init__(); self.calls = 0; self.lock = Lock()
        def upsert(self, chunks):
            with self.lock: self.calls += 1
            time.sleep(0.05)
            super().upsert(chunks)
    index = SlowIndex()
    service = IngestionService(repo, FixedEmbeddings(), index)
    def execute():
        try:
            return service.index_version(
                version["id"], embedding_profile_id=FixedEmbeddings.profile.profile_id,
                index_version="idx-v1",
            )
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert index.calls == 1
    assert sum(item is not None for item in results) == 1


def test_stale_ingestion_claim_can_be_reclaimed_and_old_worker_is_fenced(tmp_path):
    repo = Repository(tmp_path / "stale-index.db"); repo.initialize()
    version, _, _ = ingest_document(
        repo,
        DocumentSource(source_uri="https://example.com/s", source_type="filing", title="报告", publisher="交易所"),
        "正文内容", embedding_profile_id="p1", index_version="i1",
    )
    old_token = repo.claim_ingestion_job(
        version["id"], embedding_profile_id="p1", index_version="i1"
    )
    with repo.connect() as connection:
        connection.execute(
            "UPDATE ingestion_jobs SET claim_expires_at='2000-01-01T00:00:00+00:00'"
        )
    new_token = repo.claim_ingestion_job(
        version["id"], embedding_profile_id="p1", index_version="i1"
    )
    assert old_token and new_token and old_token != new_token
    with pytest.raises(ValueError, match="claim was lost"):
        repo.finish_ingestion_job(
            version["id"], embedding_profile_id="p1", index_version="i1",
            claim_token=old_token, success=True,
        )
    repo.finish_ingestion_job(
        version["id"], embedding_profile_id="p1", index_version="i1",
        claim_token=new_token, success=True,
    )
