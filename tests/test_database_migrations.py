from __future__ import annotations

import sqlite3
import pytest

from backend.database import Repository


EXPECTED_TABLES = {
    "schema_migrations",
    "cases",
    "agent_runs",
    "plans",
    "run_steps",
    "tool_calls",
    "checkpoints",
    "run_leases",
    "events",
    "evidence",
    "conversation_turns",
    "case_summaries",
    "pending_confirmations",
    "route_requests",
    "research_intakes",
    "entity_confirmations",
    "execution_authorizations",
    "tool_execution_claims",
    "execution_authorization_attempts",
}


def table_names(path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def create_legacy_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE cases (
                id TEXT PRIMARY KEY, company TEXT NOT NULL, symbol TEXT,
                market TEXT NOT NULL, title TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                company TEXT NOT NULL, symbol TEXT, market TEXT NOT NULL,
                question TEXT NOT NULL, agent TEXT NOT NULL, depth TEXT NOT NULL,
                status TEXT NOT NULL, current_step TEXT NOT NULL, progress INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result_json TEXT, error TEXT
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
                progress INTEGER NOT NULL, message TEXT NOT NULL,
                payload_json TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE evidence (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                citation_number INTEGER NOT NULL, title TEXT NOT NULL,
                publisher TEXT NOT NULL, url TEXT NOT NULL,
                source_type TEXT NOT NULL, excerpt TEXT NOT NULL,
                agent TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(task_id, citation_number)
            );
            INSERT INTO cases VALUES ('c1','腾讯控股','0700.HK','HK','腾讯研究','t','t');
            INSERT INTO tasks VALUES (
                'r1','c1','腾讯控股','0700.HK','HK','问题','financial','standard',
                'cancelled','cancelled',20,'t','t',NULL,NULL
            );
            INSERT INTO events (
                task_id,kind,step,status,progress,message,payload_json,created_at
            ) VALUES ('r1','task.cancelled','cancelled','cancelled',20,'停止',NULL,'t');
            INSERT INTO evidence VALUES (
                'e1','r1',1,'标题','来源','https://example.com','网页','摘要','agent','t'
            );
            """
        )


def test_fresh_database_has_versioned_durable_schema(tmp_path):
    path = tmp_path / "fresh.db"
    repository = Repository(path)
    repository.initialize()
    repository.initialize()

    assert EXPECTED_TABLES <= table_names(path)
    with sqlite3.connect(path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,)]
    assert journal_mode.lower() == "wal"


def test_legacy_database_is_upgraded_without_losing_run_events_or_evidence(tmp_path):
    path = tmp_path / "legacy.db"
    create_legacy_database(path)

    repository = Repository(path)
    repository.initialize()
    migrated = repository.get_task("r1")

    assert "tasks" not in table_names(path)
    assert migrated is not None
    assert migrated["status"] == "failed"
    assert "legacy cancelled" in migrated["error"]
    assert repository.list_events("r1")[0]["kind"] == "task.cancelled"
    assert migrated["evidence"][0]["citation_number"] == 1


def test_failed_legacy_migration_rolls_back_schema_changes(tmp_path):
    path = tmp_path / "broken-legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")

    with pytest.raises(sqlite3.OperationalError):
        Repository(path).initialize()

    names = table_names(path)
    assert "tasks" in names
    assert "agent_runs" not in names
    assert "schema_migrations" not in names


def test_legacy_active_and_paused_runs_receive_recovery_checkpoint(tmp_path):
    path = tmp_path / "legacy-active.db"
    create_legacy_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO tasks VALUES (
                'r2','c1','腾讯控股','0700.HK','HK','运行问题','financial','standard',
                'running','reading',50,'t','t',NULL,NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tasks VALUES (
                'r3','c1','腾讯控股','0700.HK','HK','暂停问题','financial','standard',
                'paused','reading',50,'t','t',NULL,NULL
            )
            """
        )

    repository = Repository(path)
    repository.initialize()

    for run_id in ("r2", "r3"):
        snapshot = repository.get_runtime_snapshot(run_id)
        assert snapshot["plan"]["version"] == 1
        assert snapshot["checkpoint"]["sequence"] == 0
        assert snapshot["checkpoint"]["state"]["migrated_from_legacy"] is True
    assert "r2" in repository.list_recovery_candidates()
    assert repository.get_task("r3")["status"] == "paused"


def test_version_two_backfills_active_runs_from_early_phase_one_schema(tmp_path):
    path = tmp_path / "early-v1.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        for trigger in (
            "validate_agent_run_status_insert", "validate_agent_run_status_update",
            "validate_agent_run_state_edge", "validate_agent_run_completion",
            "validate_agent_run_failure",
            "validate_completed_run_immutable_fields",
            "validate_failed_run_immutable_fields",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 2")
        connection.execute(
            "INSERT INTO cases VALUES ('c','公司',NULL,'HK','标题','t','t')"
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                id,case_id,company,market,question,agent,depth,status,current_step,
                progress,state_version,budget_used,frontier_json,recovery_required,
                created_at,updated_at
            ) VALUES ('r','c','公司','HK','问题','financial','standard','running',
                      'reading',40,1,0,'{}',0,'t','t')
            """
        )

    repository.initialize()
    snapshot = repository.get_runtime_snapshot("r")
    assert snapshot["plan"]["version"] == 1
    assert snapshot["checkpoint"]["state"]["migrated_from_legacy"] is True
    with repository.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row[0] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]


def test_version_seven_upgrades_a_version_six_database(tmp_path):
    path = tmp_path / "v6.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_authorizations")
        connection.execute("DROP TABLE entity_confirmations")
        connection.execute("DROP TABLE research_intakes")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 7")

    repository.initialize()

    assert {"research_intakes", "entity_confirmations", "execution_authorizations"} <= table_names(path)
    with repository.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == 13


def test_version_eight_upgrades_v7_authorization_data_and_is_idempotent(tmp_path):
    path = tmp_path / "v7-data.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute("DROP TABLE tool_execution_claims")
        connection.execute("ALTER TABLE execution_authorizations RENAME TO execution_authorizations_v8")
        connection.execute(
            """
            CREATE TABLE execution_authorizations (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, plan_version INTEGER NOT NULL,
                step_id TEXT NOT NULL, tool_name TEXT NOT NULL, decision TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL, estimated_cost INTEGER NOT NULL,
                budget_before INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(run_id, plan_version, step_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO execution_authorizations VALUES ('old-allow','run-a',1,'s1','search_web','allow','[\"POLICY_ALLOW\"]',3,10,'2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO execution_authorizations VALUES ('old-deny','run-b',1,'s2','search_filings','deny','[\"ENTITY_NOT_CONFIRMED\"]',2,10,'2026-01-01T00:00:00+00:00')"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version >= 8")
        connection.execute("DROP TABLE execution_authorizations_v8")
    repository.initialize()
    repository.initialize()
    with repository.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_authorizations)")}
        versions = connection.execute("SELECT version FROM schema_migrations WHERE version = 8").fetchall()
        migrated = connection.execute(
            "SELECT id, status, effective_cost, capability_token_hash FROM execution_authorizations ORDER BY id"
        ).fetchall()
    assert {"capability_token_hash", "status", "effective_cost"} <= columns
    assert [tuple(row) for row in versions] == [(8,)]
    assert [tuple(row) for row in migrated] == [
        ("old-allow", "recorded", 0, None),
        ("old-deny", "recorded", 0, None),
    ]
    assert "tool_execution_claims" in table_names(path)


def test_version_eight_malformed_claim_table_rolls_back(tmp_path):
    path = tmp_path / "broken-v8.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute("DROP TABLE tool_execution_claims")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 8")
        connection.execute("CREATE TABLE tool_execution_claims(id TEXT PRIMARY KEY)")
    with pytest.raises(sqlite3.OperationalError):
        repository.initialize()
    with repository.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_authorizations)")}
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 8"
        ).fetchone()[0] == 0
        assert "capability_token_hash" in columns
        assert set(connection.execute("PRAGMA table_info(tool_execution_claims)").fetchone()) >= {0, "id"}


def test_version_nine_upgrades_an_already_published_v8_database(tmp_path):
    path = tmp_path / "published-v8.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_authorization_attempts")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 9")
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 8
    repository.initialize()
    repository.initialize()
    with repository.connect() as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(execution_authorization_attempts)")]
        versions = connection.execute("SELECT version FROM schema_migrations WHERE version = 9").fetchall()
    assert columns == [
        "id", "authorization_id", "decision", "reason_codes_json",
        "effective_cost", "budget_before", "created_at",
    ]
    assert [tuple(row) for row in versions] == [(9,)]


def test_version_nine_malformed_attempt_table_rolls_back(tmp_path):
    path = tmp_path / "broken-v9.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_authorization_attempts")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 9")
        connection.execute("CREATE TABLE execution_authorization_attempts(id TEXT PRIMARY KEY)")
    with pytest.raises(sqlite3.OperationalError):
        repository.initialize()
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 9"
        ).fetchone()[0] == 0
        assert [row[1] for row in connection.execute(
            "PRAGMA table_info(execution_authorization_attempts)"
        )] == ["id"]


def test_version_five_upgrades_a_version_four_database(tmp_path):
    path = tmp_path / "v4.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_authorizations")
        connection.execute("DROP TABLE entity_confirmations")
        connection.execute("DROP TABLE research_intakes")
        connection.execute("DROP TABLE route_requests")
        connection.execute("DROP TABLE pending_confirmations")
        connection.execute("DROP TABLE case_summaries")
        connection.execute("DROP TABLE conversation_turns")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 5")

    repository.initialize()

    assert {"conversation_turns", "case_summaries", "pending_confirmations"} <= table_names(path)


def test_version_five_migration_rolls_back_on_malformed_existing_table(tmp_path):
    path = tmp_path / "broken-v5.db"
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_authorizations")
        connection.execute("DROP TABLE entity_confirmations")
        connection.execute("DROP TABLE research_intakes")
        connection.execute("DROP TABLE route_requests")
        connection.execute("DROP TABLE pending_confirmations")
        connection.execute("DROP TABLE case_summaries")
        connection.execute("DROP TABLE conversation_turns")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 5")
        connection.execute("CREATE TABLE conversation_turns(id TEXT PRIMARY KEY)")

    with pytest.raises(sqlite3.OperationalError):
        repository.initialize()

    with repository.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row[0] for row in versions] == [1, 2, 3, 4]
    assert "case_summaries" not in table_names(path)
    assert "pending_confirmations" not in table_names(path)
