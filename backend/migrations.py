from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


LATEST_SCHEMA_VERSION = 7


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _create_base_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            symbol TEXT,
            market TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES cases(id),
            idempotency_key TEXT UNIQUE,
            company TEXT NOT NULL,
            symbol TEXT,
            market TEXT NOT NULL,
            question TEXT NOT NULL,
            agent TEXT NOT NULL,
            depth TEXT NOT NULL,
            status TEXT NOT NULL,
            current_step TEXT NOT NULL,
            progress INTEGER NOT NULL,
            state_version INTEGER NOT NULL DEFAULT 1,
            budget_used INTEGER NOT NULL DEFAULT 0,
            frontier_json TEXT NOT NULL DEFAULT '{}',
            recovery_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            result_json TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, version)
        );

        CREATE TABLE IF NOT EXISTS run_steps (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            plan_version INTEGER NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            input_json TEXT,
            output_json TEXT,
            error TEXT,
            idempotency_key TEXT NOT NULL,
            commit_fingerprint TEXT,
            attempt INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step_id TEXT REFERENCES run_steps(id) ON DELETE CASCADE,
            tool_name TEXT NOT NULL,
            tool_version TEXT NOT NULL,
            status TEXT NOT NULL,
            input_json TEXT,
            output_json TEXT,
            error TEXT,
            duration_ms INTEGER,
            cost_units INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            state_version INTEGER NOT NULL,
            plan_version INTEGER NOT NULL,
            frontier_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        );

        CREATE TABLE IF NOT EXISTS run_leases (
            run_id TEXT PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            lease_token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            step TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            citation_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            publisher TEXT NOT NULL,
            url TEXT NOT NULL,
            source_type TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            agent TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, citation_number)
        );

        CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id, id);
        CREATE INDEX IF NOT EXISTS idx_runs_case_id ON agent_runs(case_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_status ON agent_runs(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_steps_run_id ON run_steps(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_run_id ON tool_calls(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_checkpoints_run_id ON checkpoints(run_id, sequence DESC);
        """
    )


def _upgrade_legacy_schema(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE tasks RENAME TO agent_runs")
    connection.execute("ALTER TABLE events RENAME COLUMN task_id TO run_id")
    connection.execute("ALTER TABLE evidence RENAME COLUMN task_id TO run_id")
    connection.execute("ALTER TABLE agent_runs ADD COLUMN idempotency_key TEXT")
    connection.execute("ALTER TABLE agent_runs ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1")
    connection.execute("ALTER TABLE agent_runs ADD COLUMN budget_used INTEGER NOT NULL DEFAULT 0")
    connection.execute("ALTER TABLE agent_runs ADD COLUMN frontier_json TEXT NOT NULL DEFAULT '{}'")
    connection.execute("ALTER TABLE agent_runs ADD COLUMN recovery_required INTEGER NOT NULL DEFAULT 0")
    connection.execute(
        """
        UPDATE agent_runs
        SET status = 'running', current_step = CASE WHEN current_step = 'queued' THEN 'starting' ELSE current_step END
        WHERE status = 'queued'
        """
    )
    connection.execute(
        """
        UPDATE agent_runs
        SET status = 'failed', current_step = 'failed',
            error = COALESCE(error || '; ', '') || 'Migrated from legacy cancelled state'
        WHERE status = 'cancelled'
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency_key ON agent_runs(idempotency_key)"
    )
    _create_base_schema(connection)


def _backfill_active_runs(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT * FROM agent_runs
        WHERE status IN ('running', 'pause_requested', 'paused', 'resuming')
          AND NOT EXISTS (SELECT 1 FROM checkpoints WHERE checkpoints.run_id = agent_runs.id)
        """
    ).fetchall()
    for run in rows:
        plan_id = f"legacy-plan:{run['id']}"
        checkpoint_id = f"legacy-checkpoint:{run['id']}"
        frontier = (
            '{"plan_version":1,"ready_step_ids":[],"running_step_ids":[],'
            '"blocked_step_ids":[],"completed_step_ids":[]}'
        )
        plan = '{"version":1,"goal":' + _quote_json(run["question"]) + ',"steps":[]}'
        state = (
            '{"plan_version":1,"frontier":' + frontier +
            ',"budget_used":0,"migrated_from_legacy":true}'
        )
        connection.execute(
            "INSERT OR IGNORE INTO plans(id, run_id, version, plan_json, created_at) VALUES (?, ?, 1, ?, ?)",
            (plan_id, run["id"], plan, run["updated_at"]),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO checkpoints(
                id, run_id, sequence, state_version, plan_version,
                frontier_json, state_json, created_at
            ) VALUES (?, ?, 0, ?, 1, ?, ?, ?)
            """,
            (
                checkpoint_id, run["id"], run["state_version"], frontier,
                state, run["updated_at"],
            ),
        )
        connection.execute(
            "UPDATE agent_runs SET frontier_json = ? WHERE id = ?",
            (frontier, run["id"]),
        )


def _quote_json(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _create_status_triggers(connection: sqlite3.Connection) -> None:
    allowed = "'running','pause_requested','paused','resuming','failed','completed'"
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS validate_agent_run_status_insert
        BEFORE INSERT ON agent_runs
        WHEN NEW.status NOT IN ({allowed})
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent run status');
        END
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS validate_agent_run_status_update
        BEFORE UPDATE OF status ON agent_runs
        WHEN NEW.status NOT IN ({allowed})
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent run status');
        END
        """
    )


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _add_transition_guards(connection: sqlite3.Connection) -> None:
    if not _column_exists(connection, "run_steps", "commit_fingerprint"):
        connection.execute("ALTER TABLE run_steps ADD COLUMN commit_fingerprint TEXT")
    allowed = "'running','pause_requested','paused','resuming','failed','completed'"
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS validate_agent_run_state_edge
        BEFORE UPDATE OF status ON agent_runs
        WHEN NEW.status IN ({allowed})
          AND NEW.status != OLD.status AND NOT (
            (OLD.status = 'running' AND NEW.status IN ('pause_requested','failed','completed')) OR
            (OLD.status = 'pause_requested' AND NEW.status IN ('paused','failed')) OR
            (OLD.status = 'paused' AND NEW.status = 'resuming') OR
            (OLD.status = 'resuming' AND NEW.status IN ('running','failed')) OR
            (
                OLD.status IN ('running','pause_requested')
                AND NEW.status = 'resuming'
                AND NEW.recovery_required = 1
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'illegal agent run state transition');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_agent_run_completion
        BEFORE UPDATE OF status ON agent_runs
        WHEN NEW.status = 'completed' AND (
            NEW.progress != 100 OR NEW.result_json IS NULL OR NOT EXISTS (
                SELECT 1 FROM checkpoints
                WHERE checkpoints.run_id = NEW.id
                  AND checkpoints.state_version = NEW.state_version
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'completed run requires result and matching checkpoint');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_agent_run_failure
        BEFORE UPDATE OF status ON agent_runs
        WHEN NEW.status = 'failed' AND (NEW.error IS NULL OR length(NEW.error) = 0)
        BEGIN
            SELECT RAISE(ABORT, 'failed run requires an error');
        END
        """
    )


def _add_terminal_mutation_guards(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_completed_run_immutable_fields
        BEFORE UPDATE OF result_json, progress, error ON agent_runs
        WHEN OLD.status = 'completed' AND (
            NEW.result_json IS NOT OLD.result_json OR
            NEW.progress != OLD.progress OR
            NEW.error IS NOT OLD.error
        )
        BEGIN
            SELECT RAISE(ABORT, 'completed run execution fields are immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_failed_run_immutable_fields
        BEFORE UPDATE OF result_json, progress, error ON agent_runs
        WHEN OLD.status = 'failed' AND (
            NEW.result_json IS NOT OLD.result_json OR
            NEW.progress != OLD.progress OR
            NEW.error IS NOT OLD.error
        )
        BEGIN
            SELECT RAISE(ABORT, 'failed run execution fields are immutable');
        END
        """
    )


def _add_short_term_memory_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            intent TEXT,
            reason_codes_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            UNIQUE(case_id, sequence)
        );

        CREATE TABLE IF NOT EXISTS case_summaries (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            summary TEXT NOT NULL,
            last_turn_sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(case_id, version)
        );

        CREATE TABLE IF NOT EXISTS pending_confirmations (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            prompt TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','resolved','expired','superseded')),
            expires_at TEXT NOT NULL,
            resolved_value_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_turns_case
            ON conversation_turns(case_id, sequence DESC);
        CREATE INDEX IF NOT EXISTS idx_case_summaries_case
            ON case_summaries(case_id, version DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_confirmation_case
            ON pending_confirmations(case_id) WHERE status = 'pending';
        """,
    )


def _add_route_request_ledger(connection: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    for row in connection.execute(
        "SELECT id, status, expires_at FROM pending_confirmations"
    ).fetchall():
        try:
            parsed = datetime.fromisoformat(row["expires_at"])
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("naive datetime")
            canonical = parsed.astimezone(timezone.utc)
            status = "expired" if row["status"] == "pending" and canonical <= now else row["status"]
        except (TypeError, ValueError):
            canonical = now
            status = "expired" if row["status"] == "pending" else row["status"]
        canonical_expires_at = canonical.isoformat()
        if canonical_expires_at != row["expires_at"] or status != row["status"]:
            connection.execute(
                "UPDATE pending_confirmations SET expires_at = ?, status = ?, updated_at = ? WHERE id = ?",
                (canonical_expires_at, status, now.isoformat(), row["id"]),
            )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS route_requests (
            id TEXT PRIMARY KEY,
            case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
            message TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            response TEXT NOT NULL,
            trace_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_route_requests_case ON route_requests(case_id, created_at)"
    )


def _add_research_intake_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS research_intakes (
            id TEXT PRIMARY KEY,
            route_request_id TEXT NOT NULL UNIQUE REFERENCES route_requests(id),
            message TEXT NOT NULL,
            depth TEXT NOT NULL CHECK(depth IN ('quick','standard','deep')),
            budget_limit INTEGER NOT NULL CHECK(budget_limit > 0),
            status TEXT NOT NULL CHECK(status IN (
                'awaiting_confirmation','ready','needs_clarification','running','failed'
            )),
            entity_query TEXT,
            candidates_json TEXT NOT NULL DEFAULT '[]',
            resolved_entity_json TEXT,
            run_id TEXT UNIQUE REFERENCES agent_runs(id),
            replan_count INTEGER NOT NULL DEFAULT 0 CHECK(replan_count BETWEEN 0 AND 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entity_confirmations (
            id TEXT PRIMARY KEY,
            intake_id TEXT NOT NULL UNIQUE REFERENCES research_intakes(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('pending','resolved','expired')),
            candidates_json TEXT NOT NULL,
            selected_candidate_id TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS execution_authorizations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            plan_version INTEGER NOT NULL,
            step_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('allow','deny')),
            reason_codes_json TEXT NOT NULL,
            estimated_cost INTEGER NOT NULL CHECK(estimated_cost >= 0),
            budget_before INTEGER NOT NULL CHECK(budget_before >= 0),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, plan_version, step_id)
        );

        CREATE INDEX IF NOT EXISTS idx_research_intakes_status
            ON research_intakes(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_entity_confirmations_status
            ON entity_confirmations(status, expires_at);
        CREATE INDEX IF NOT EXISTS idx_execution_authorizations_run
            ON execution_authorizations(run_id, plan_version);
        """,
    )


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        current = int(row[0] or 0)
        if current < 1:
            if _table_exists(connection, "tasks") and not _table_exists(connection, "agent_runs"):
                _upgrade_legacy_schema(connection)
            else:
                _create_base_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (1,),
            )
            current = 1
        if current < 2:
            _backfill_active_runs(connection)
            _create_status_triggers(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
            current = 2
        if current < 3:
            _add_transition_guards(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")
            current = 3
        if current < 4:
            _add_terminal_mutation_guards(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (4)")
            current = 4
        if current < 5:
            _add_short_term_memory_schema(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (5)")
            current = 5
        if current < 6:
            _add_route_request_ledger(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (6)")
            current = 6
        if current < 7:
            _add_research_intake_schema(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (7)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
