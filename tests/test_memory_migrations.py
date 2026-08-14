from __future__ import annotations

import sqlite3

import pytest

from backend.database import Repository
from backend.migrations import LATEST_SCHEMA_VERSION, migrate
from backend.schemas import MemoryCandidate, MemoryScope


MEMORY_TABLES = {
    "memory_records", "memory_versions", "memory_evidence", "memory_events",
    "memory_deletion_jobs", "memory_write_requests", "memory_activation_authorizations",
}
MEMORY_DROP_ORDER = [
    "memory_activation_authorizations", "memory_write_requests", "memory_evidence", "memory_events",
    "memory_deletion_jobs", "memory_versions", "memory_records",
]


def test_fresh_database_creates_schema_v13(tmp_path):
    repo = Repository(tmp_path / "memory.db")
    repo.initialize()
    with repo.connect() as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert MEMORY_TABLES <= tables
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 13
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_real_v11_shape_upgrades_once(tmp_path):
    repo = Repository(tmp_path / "upgrade.db")
    repo.initialize()
    with repo.connect() as connection:
        for table in MEMORY_DROP_ORDER:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 12")
    repo.initialize()
    repo.initialize()
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=12").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=13").fetchone()[0] == 1
        assert {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )} >= MEMORY_TABLES


def test_malformed_v12_rolls_back_without_recording_version(tmp_path):
    repo = Repository(tmp_path / "broken.db")
    repo.initialize()
    with repo.connect() as connection:
        for table in MEMORY_DROP_ORDER:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 12")
        connection.execute("CREATE TABLE memory_records(id TEXT PRIMARY KEY)")
    with repo.connect() as connection, pytest.raises(sqlite3.OperationalError):
        migrate(connection)
    with repo.connect() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11
        assert [row[1] for row in connection.execute("PRAGMA table_info(memory_records)")] == ["id"]


def test_future_schema_is_rejected(tmp_path):
    repo = Repository(tmp_path / "future.db")
    repo.initialize()
    with repo.connect() as connection:
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (LATEST_SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError, match="newer than supported"):
        repo.initialize()


def test_memory_constraints_and_transition_guards(tmp_path):
    repo = Repository(tmp_path / "constraints.db")
    repo.initialize()
    with repo.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memory_records(id,memory_key,memory_type,scope_kind,scope_hash,tenant_id,created_at,updated_at) VALUES ('m','k','invented','user',?, 't','n','n')",
                ("0" * 64,),
            )
        connection.execute(
            "INSERT INTO memory_records(id,memory_key,memory_type,scope_kind,scope_hash,tenant_id,user_id,created_at,updated_at) VALUES ('m','k','user_preference','user',?,'t','u','n','n')",
            ("0" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="must start as candidate"):
            connection.execute(
                """
                INSERT INTO memory_versions(
                    id,memory_id,version,status,content_json,content_text,content_sha256,
                    request_fingerprint,idempotency_key,confidence,created_at,updated_at
                ) VALUES ('v','m',1,'active','{}','b',?,?, 'i',1,'n','n')
                """,
                ("1" * 64, "2" * 64),
            )


def test_strict_memory_models_enforce_scope_and_write_policy():
    with pytest.raises(ValueError, match="company and market"):
        MemoryScope(scope_kind="public_company", tenant_id="public")
    with pytest.raises(ValueError, match="persisted claims and evidence"):
        MemoryCandidate(
            memory_type="company_fact", memory_key="revenue", content={"value": 1},
            content_text="收入为1", idempotency_key="x", confidence=.9,
            scope=MemoryScope(
                scope_kind="public_company", tenant_id="public", company="腾讯", market="HK"
            ),
        )
