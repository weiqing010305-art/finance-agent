from __future__ import annotations

import sqlite3

import pytest

from backend.database import Repository
from backend.migrations import LATEST_SCHEMA_VERSION, migrate


PHASE4_TABLES = {
    "documents",
    "document_versions",
    "document_chunks",
    "ingestion_jobs",
    "evidence_items",
    "claims",
    "claim_evidence",
    "report_generations",
    "report_snapshots",
    "reports",
    "report_citations",
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_fresh_database_preserves_phase4_schema_under_v13(tmp_path):
    path = tmp_path / "fresh.db"
    Repository(path).initialize()
    with sqlite3.connect(path) as connection:
        assert LATEST_SCHEMA_VERSION == 13
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 13
        assert PHASE4_TABLES <= _tables(connection)
        connection.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("d1", "https://example.com/a", "filing", "A", "Exchange", "public", "公司", "1", "CN", "t", "t"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO document_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("v1", "d1", "bad", None, "text/plain", 1, None, "t", "x", "t"),
            )


def test_real_v9_database_upgrades_without_changing_existing_data(tmp_path):
    path = tmp_path / "v9.db"
    repo = Repository(path)
    repo.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE report_citations")
        for table in sorted(PHASE4_TABLES - {"report_citations"}):
            connection.execute(f"DROP TABLE {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 10")
        connection.execute(
            "INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
            ("legacy-case", "腾讯", "0700.HK", "HK", "旧数据", "t", "t"),
        )
        connection.commit()
    repo.initialize()
    repo.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT company FROM cases WHERE id='legacy-case'"
        ).fetchone()[0] == "腾讯"
        assert PHASE4_TABLES <= _tables(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=10"
        ).fetchone()[0] == 1


def test_malformed_phase4_table_rolls_back_version_10(tmp_path):
    path = tmp_path / "malformed.db"
    repo = Repository(path)
    repo.initialize()
    with sqlite3.connect(path) as connection:
        for table in reversed(tuple(PHASE4_TABLES)):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("DELETE FROM schema_migrations WHERE version>=10")
        connection.execute("CREATE TABLE ingestion_jobs(id TEXT PRIMARY KEY)")
        connection.commit()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.OperationalError):
            migrate(connection)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 9
        assert [row[1] for row in connection.execute("PRAGMA table_info(ingestion_jobs)")] == ["id"]


def test_future_schema_version_is_rejected_fail_closed(tmp_path):
    path = tmp_path / "future.db"
    repo = Repository(path); repo.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO schema_migrations(version) VALUES (999)")
    with pytest.raises(RuntimeError, match="newer than supported"):
        repo.initialize()
