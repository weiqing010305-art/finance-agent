from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


LATEST_SCHEMA_VERSION = 13


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _split_sql_statements(script: str) -> list[str]:
    """Split SQL on top-level semicolons, ignoring those inside string
    literals ('' escaped), ``--`` line comments and ``/* */`` block comments.

    A naive ``script.split(';')`` breaks any statement whose string literal
    or comment contains a semicolon, silently corrupting the migration.
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        nxt = script[index + 1] if index + 1 < length else ""
        if in_block_comment:
            current.append(char)
            if char == "*" and nxt == "/":
                current.append(nxt)
                index += 1
                in_block_comment = False
            index += 1
            continue
        if in_line_comment:
            current.append(char)
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_string:
            current.append(char)
            if char == "'":
                if nxt == "'":  # '' escaped quote inside the literal
                    current.append(nxt)
                    index += 1
                else:
                    in_string = False
            index += 1
            continue
        if char == "-" and nxt == "-":
            in_line_comment = True
            current.append(char)
            index += 1
            continue
        if char == "/" and nxt == "*":
            in_block_comment = True
            current.append(char)
            index += 1
            continue
        if char == "'":
            in_string = True
            current.append(char)
            index += 1
            continue
        if char == ";":
            statements.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if current:
        statements.append("".join(current))
    return [statement.strip() for statement in statements if statement.strip()]


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    for statement in _split_sql_statements(script):
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
    # Generated from the shared state-machine constant so the trigger can
    # never drift from the Python CAS guards (see backend/run_states.py).
    from collections import defaultdict
    from backend.run_states import RECOVERY_TRANSITIONS, RUN_STATE_TRANSITIONS

    def clauses(edges) -> str:
        by_old: dict[str, list[str]] = defaultdict(list)
        for old, new in sorted(edges):
            by_old[old].append(new)
        parts = []
        for old in sorted(by_old):
            news = ", ".join(f"'{n}'" for n in sorted(set(by_old[old])))
            parts.append(f"(OLD.status = '{old}' AND NEW.status IN ({news}))")
        return " OR ".join(parts)

    normal_edges = clauses(RUN_STATE_TRANSITIONS)
    recovery_edges = clauses(RECOVERY_TRANSITIONS)
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS validate_agent_run_state_edge
        BEFORE UPDATE OF status ON agent_runs
        WHEN NEW.status IN ({allowed})
          AND NEW.status != OLD.status AND NOT (
            ({normal_edges}) OR
            ({recovery_edges} AND NEW.recovery_required = 1)
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


def _add_tool_execution_ledger(connection: sqlite3.Connection) -> None:
    for column, definition in (
        ("capability_token_hash", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'recorded'"),
        ("effective_cost", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if not _column_exists(connection, "execution_authorizations", column):
            connection.execute(
                f"ALTER TABLE execution_authorizations ADD COLUMN {column} {definition}"
            )
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS tool_execution_claims (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            plan_version INTEGER NOT NULL,
            step_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            authorization_id TEXT NOT NULL REFERENCES execution_authorizations(id),
            execution_token_hash TEXT NOT NULL,
            lease_token_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('claimed','observed','committed','failed')),
            input_json TEXT NOT NULL,
            output_json TEXT,
            error TEXT,
            duration_ms INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, plan_version, step_id),
            UNIQUE(run_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_tool_execution_claims_run
            ON tool_execution_claims(run_id, status, plan_version);
        """,
    )


def _add_authorization_attempt_history(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS execution_authorization_attempts (
            id TEXT PRIMARY KEY,
            authorization_id TEXT NOT NULL REFERENCES execution_authorizations(id) ON DELETE CASCADE,
            decision TEXT NOT NULL CHECK(decision IN ('allow','deny')),
            reason_codes_json TEXT NOT NULL,
            effective_cost INTEGER NOT NULL,
            budget_before INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_authorization_attempts_auth
            ON execution_authorization_attempts(authorization_id, created_at);
        """,
    )


def _add_phase4_rag_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            source_uri TEXT NOT NULL,
            source_type TEXT NOT NULL,
            title TEXT NOT NULL,
            publisher TEXT NOT NULL,
            access_scope TEXT NOT NULL CHECK(length(access_scope) > 0),
            company TEXT,
            symbol TEXT,
            market TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_uri, access_scope)
        );
        CREATE TABLE IF NOT EXISTS document_versions (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            source_version TEXT,
            mime_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK(byte_size > 0),
            published_at TEXT,
            fetched_at TEXT NOT NULL,
            normalized_text TEXT NOT NULL CHECK(length(normalized_text) > 0),
            created_at TEXT NOT NULL,
            UNIQUE(document_id, content_sha256)
        );
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY,
            document_version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            section TEXT,
            page INTEGER CHECK(page IS NULL OR page > 0),
            text TEXT NOT NULL CHECK(length(text) > 0),
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            char_start INTEGER NOT NULL CHECK(char_start >= 0),
            char_end INTEGER NOT NULL CHECK(char_end > char_start),
            created_at TEXT NOT NULL,
            UNIQUE(document_version_id, ordinal),
            UNIQUE(document_version_id, content_sha256, char_start)
        );
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            id TEXT PRIMARY KEY,
            document_version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            embedding_profile_id TEXT NOT NULL,
            index_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','indexing','indexed','failed')),
            attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(document_version_id, embedding_profile_id, index_version)
        );
        CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status
            ON ingestion_jobs(status, updated_at);
        CREATE TABLE IF NOT EXISTS evidence_items (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            document_version_id TEXT REFERENCES document_versions(id),
            chunk_id TEXT REFERENCES document_chunks(id),
            source_uri TEXT NOT NULL,
            title TEXT NOT NULL,
            publisher TEXT NOT NULL,
            source_type TEXT NOT NULL,
            excerpt TEXT NOT NULL CHECK(length(excerpt) > 0),
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            access_scope TEXT NOT NULL,
            authority_tier INTEGER NOT NULL CHECK(authority_tier BETWEEN 0 AND 5),
            published_at TEXT,
            retrieved_at TEXT NOT NULL,
            page INTEGER CHECK(page IS NULL OR page > 0),
            section TEXT,
            company TEXT,
            period TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, content_sha256, source_uri)
        );
        CREATE TABLE IF NOT EXISTS claims (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            text TEXT NOT NULL CHECK(length(text) > 0),
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            status TEXT NOT NULL CHECK(status IN ('supported','partially_supported','unsupported','conflicted')),
            confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
            period TEXT,
            unit TEXT,
            currency TEXT,
            reason_codes_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, content_sha256)
        );
        CREATE TABLE IF NOT EXISTS claim_evidence (
            claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES evidence_items(id) ON DELETE CASCADE,
            relation TEXT NOT NULL CHECK(relation IN ('supports','partially_supports','conflicts')),
            created_at TEXT NOT NULL,
            PRIMARY KEY(claim_id, evidence_id)
        );
        CREATE TABLE IF NOT EXISTS report_generations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            generation_key TEXT NOT NULL,
            model TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version > 0),
            status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, generation_key)
        );
        CREATE TABLE IF NOT EXISTS report_snapshots (
            id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL REFERENCES report_generations(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence >= 0),
            snapshot_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            created_at TEXT NOT NULL,
            UNIQUE(generation_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(id) ON DELETE CASCADE,
            generation_id TEXT NOT NULL REFERENCES report_generations(id),
            markdown TEXT NOT NULL,
            report_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            degraded INTEGER NOT NULL DEFAULT 0 CHECK(degraded IN (0,1)),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS report_citations (
            report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            citation_number INTEGER NOT NULL CHECK(citation_number > 0),
            claim_id TEXT NOT NULL REFERENCES claims(id),
            evidence_id TEXT NOT NULL REFERENCES evidence_items(id),
            PRIMARY KEY(report_id, citation_number),
            UNIQUE(report_id, claim_id, evidence_id)
        );
        """,
    )


def _add_ingestion_claim_fencing(connection: sqlite3.Connection) -> None:
    for column, definition in (
        ("claim_token_hash", "TEXT"),
        ("claim_expires_at", "TEXT"),
    ):
        if not _column_exists(connection, "ingestion_jobs", column):
            connection.execute(
                f"ALTER TABLE ingestion_jobs ADD COLUMN {column} {definition}"
            )


def _add_long_term_memory_schema(connection: sqlite3.Connection) -> None:
    required = {
        "memory_records": {"id", "scope_hash", "memory_key", "tombstoned"},
        "memory_versions": {"id", "memory_id", "status", "content_sha256", "expires_at"},
        "memory_write_requests": {"idempotency_key", "memory_version_id"},
        "memory_evidence": {"memory_version_id", "evidence_id", "claim_id"},
        "memory_events": {"memory_id", "kind", "reason_code"},
        "memory_deletion_jobs": {"id", "scope_hash", "status", "claim_token_hash"},
    }
    existing = {name for name in required if _table_exists(connection, name)}
    if existing:
        if existing != set(required):
            raise sqlite3.OperationalError("incomplete long-term memory schema")
        for table, columns in required.items():
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise sqlite3.OperationalError(f"malformed long-term memory table: {table}")
        return
    _execute_statements(
        connection,
        """
        CREATE TABLE memory_records (
            id TEXT PRIMARY KEY,
            memory_key TEXT NOT NULL CHECK(length(memory_key) > 0),
            memory_type TEXT NOT NULL CHECK(memory_type IN (
                'company_fact','entity_identity','user_preference',
                'case_summary','task_experience'
            )),
            scope_kind TEXT NOT NULL CHECK(scope_kind IN ('public_company','user','case','system')),
            scope_hash TEXT NOT NULL CHECK(length(scope_hash) = 64),
            tenant_id TEXT NOT NULL CHECK(length(tenant_id) > 0),
            user_id TEXT,
            case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
            company TEXT,
            symbol TEXT,
            market TEXT,
            tombstoned INTEGER NOT NULL DEFAULT 0 CHECK(tombstoned IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope_hash, memory_key)
        );
        CREATE INDEX idx_memory_records_scope
            ON memory_records(scope_hash, memory_type, tombstoned);

        CREATE TABLE memory_versions (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK(version > 0),
            status TEXT NOT NULL CHECK(status IN (
                'candidate','verified','active','conflicted','rejected',
                'superseded','expired','deleted'
            )),
            content_json TEXT NOT NULL,
            content_text TEXT NOT NULL CHECK(length(content_text) > 0),
            content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) > 0),
            confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
            period TEXT,
            source_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
            source_summary_id TEXT REFERENCES case_summaries(id) ON DELETE SET NULL,
            supersedes_version_id TEXT REFERENCES memory_versions(id),
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(memory_id, version),
            UNIQUE(idempotency_key)
        );
        CREATE UNIQUE INDEX idx_memory_one_active_version
            ON memory_versions(memory_id) WHERE status = 'active';
        CREATE INDEX idx_memory_versions_read
            ON memory_versions(memory_id, status, expires_at);

        CREATE TABLE memory_write_requests (
            idempotency_key TEXT PRIMARY KEY,
            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
            memory_version_id TEXT NOT NULL REFERENCES memory_versions(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE memory_evidence (
            memory_version_id TEXT NOT NULL REFERENCES memory_versions(id) ON DELETE CASCADE,
            evidence_id TEXT NOT NULL REFERENCES evidence_items(id) ON DELETE RESTRICT,
            claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(memory_version_id, evidence_id, claim_id)
        );

        CREATE TABLE memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
            memory_version_id TEXT REFERENCES memory_versions(id) ON DELETE SET NULL,
            kind TEXT NOT NULL CHECK(length(kind) > 0),
            reason_code TEXT NOT NULL CHECK(length(reason_code) > 0),
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_memory_events_record ON memory_events(memory_id, id);

        CREATE TABLE memory_deletion_jobs (
            id TEXT PRIMARY KEY,
            memory_id TEXT REFERENCES memory_records(id) ON DELETE SET NULL,
            scope_hash TEXT NOT NULL CHECK(length(scope_hash) = 64),
            status TEXT NOT NULL CHECK(status IN ('pending','claimed','completed','failed')),
            idempotency_key TEXT NOT NULL UNIQUE,
            claim_token_hash TEXT,
            claim_expires_at TEXT,
            attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_memory_deletion_jobs_status
            ON memory_deletion_jobs(status, updated_at);

        """,
    )
    connection.execute(
        """
        CREATE TRIGGER validate_memory_version_edge
        BEFORE UPDATE OF status ON memory_versions
        WHEN NEW.status != OLD.status AND NOT (
            (OLD.status = 'candidate' AND NEW.status IN ('verified','rejected','conflicted','deleted')) OR
            (OLD.status = 'verified' AND NEW.status IN ('active','rejected','conflicted','deleted')) OR
            (OLD.status = 'active' AND NEW.status IN ('superseded','conflicted','expired','deleted')) OR
            (OLD.status = 'conflicted' AND NEW.status IN ('active','rejected','superseded','expired','deleted')) OR
            (OLD.status IN ('rejected','superseded','expired') AND NEW.status = 'deleted')
        )
        BEGIN
            SELECT RAISE(ABORT, 'illegal memory version transition');
        END
        """
    )


def _add_memory_activation_guards(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, "memory_activation_authorizations"):
        actual = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(memory_activation_authorizations)"
            )
        }
        if not {"memory_version_id", "authorization_kind", "source_fingerprint", "created_at"} <= actual:
            raise sqlite3.OperationalError("malformed memory activation authorization table")
    else:
        connection.execute(
            """
            CREATE TABLE memory_activation_authorizations (
                memory_version_id TEXT PRIMARY KEY REFERENCES memory_versions(id) ON DELETE CASCADE,
                authorization_kind TEXT NOT NULL CHECK(authorization_kind IN (
                    'verified_evidence','explicit_user_confirmation','persisted_summary','completed_run'
                )),
                source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint) = 64),
                created_at TEXT NOT NULL
            )
            """
        )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_memory_version_initial_status
        BEFORE INSERT ON memory_versions
        WHEN NEW.status != 'candidate'
        BEGIN
            SELECT RAISE(ABORT, 'memory version must start as candidate');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_memory_version_activation_authorized
        BEFORE UPDATE OF status ON memory_versions
        WHEN NEW.status = 'active' AND OLD.status != 'active' AND NOT EXISTS (
            SELECT 1 FROM memory_activation_authorizations a
            WHERE a.memory_version_id = NEW.id
        )
        BEGIN
            SELECT RAISE(ABORT, 'memory activation requires authorization ledger');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_company_fact_activation_evidence
        BEFORE UPDATE OF status ON memory_versions
        WHEN NEW.status = 'active'
          AND (SELECT memory_type FROM memory_records WHERE id=NEW.memory_id) = 'company_fact'
          AND NOT EXISTS (
              SELECT 1 FROM memory_evidence me
              WHERE me.memory_version_id=NEW.id
          )
        BEGIN
            SELECT RAISE(ABORT, 'company fact activation requires evidence links');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_memory_version_content_immutable
        BEFORE UPDATE OF content_json, content_text, content_sha256, request_fingerprint,
                         idempotency_key, memory_id, version, source_run_id,
                         source_summary_id, confidence, period, supersedes_version_id
                         ON memory_versions
        WHEN NEW.content_json IS NOT OLD.content_json
          OR NEW.content_text IS NOT OLD.content_text
          OR NEW.content_sha256 IS NOT OLD.content_sha256
          OR NEW.request_fingerprint IS NOT OLD.request_fingerprint
          OR NEW.idempotency_key IS NOT OLD.idempotency_key
          OR NEW.memory_id IS NOT OLD.memory_id
          OR NEW.source_run_id IS NOT OLD.source_run_id
          OR NEW.source_summary_id IS NOT OLD.source_summary_id
          OR NEW.confidence != OLD.confidence
          OR NEW.period IS NOT OLD.period
          OR NEW.supersedes_version_id IS NOT OLD.supersedes_version_id
          OR NEW.version != OLD.version
        BEGIN
            SELECT RAISE(ABORT, 'memory version identity is immutable');
        END
        """
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
        if current > LATEST_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {current} is newer than supported {LATEST_SCHEMA_VERSION}"
            )
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
            current = 7
        if current < 8:
            _add_tool_execution_ledger(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (8)")
            current = 8
        if current < 9:
            _add_authorization_attempt_history(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (9)")
            current = 9
        if current < 10:
            _add_phase4_rag_schema(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (10)")
            current = 10
        if current < 11:
            _add_ingestion_claim_fencing(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (11)")
            current = 11
        if current < 12:
            _add_long_term_memory_schema(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (12)")
            current = 12
        if current < 13:
            _add_memory_activation_guards(connection)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (13)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
