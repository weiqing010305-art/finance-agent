from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from backend.migrations import migrate
from backend.redaction import redact_url
from backend.schemas import ResearchCreate


TERMINAL_STATUSES = {"completed", "failed"}
SIX_RUN_STATES = {
    "running", "pause_requested", "paused", "resuming", "failed", "completed"
}
LEGAL_TRANSITIONS = {
    ("running", "pause_requested"),
    ("pause_requested", "paused"),
    ("paused", "resuming"),
    ("resuming", "running"),
    ("running", "failed"),
    ("pause_requested", "failed"),
    ("resuming", "failed"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("datetime must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_public_url(value: str) -> str:
    return redact_url(value)


def _commit_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class Repository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            migrate(connection)

    def create_run_atomic(
        self,
        request: ResearchCreate,
        *,
        owner_id: str,
        idempotency_key: str,
        lease_token: str,
        lease_expires_at: str,
    ) -> tuple[dict[str, Any], str, bool]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM agent_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_run = connection.execute(
                    "SELECT * FROM agent_runs WHERE id = ?", (existing["id"],)
                ).fetchone()
                assert existing_run is not None
                request_identity = (
                    request.company, request.symbol, request.market, request.question,
                    request.agent, request.depth,
                )
                stored_identity = (
                    existing_run["company"], existing_run["symbol"], existing_run["market"],
                    existing_run["question"], existing_run["agent"], existing_run["depth"],
                )
                if request_identity != stored_identity:
                    raise ValueError("idempotency key was already used with a different request")
                lease = connection.execute(
                    "SELECT lease_token FROM run_leases WHERE run_id = ?",
                    (existing["id"],),
                ).fetchone()
                if (
                    lease is None
                    and existing_run["status"] not in TERMINAL_STATUSES
                    and existing_run["status"] != "paused"
                ):
                    raise RuntimeError("idempotent run exists without its initial lease")
                run = self._get_task(connection, existing["id"])
                if run is None:
                    raise KeyError(existing["id"])
                return run, str(lease["lease_token"]) if lease else "", False

            run_id = str(uuid4())
            case_id = str(uuid4())
            plan_id = str(uuid4())
            checkpoint_id = str(uuid4())
            title = f"{request.company}公司研究"
            frontier = {
                "plan_version": 1,
                "ready_step_ids": [],
                "running_step_ids": [],
                "blocked_step_ids": [],
                "completed_step_ids": [],
            }
            plan = {"version": 1, "goal": request.question, "steps": []}
            state = {
                "goal": request.question,
                "plan_version": 1,
                "frontier": frontier,
                "budget_used": 0,
            }
            connection.execute(
                "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)",
                (case_id, request.company, request.symbol, request.market, title, now, now),
            )
            connection.execute(
                """
                INSERT INTO agent_runs (
                    id, case_id, idempotency_key, company, symbol, market, question,
                    agent, depth, status, current_step, progress, state_version,
                    budget_used, frontier_json, recovery_required, created_at,
                    updated_at, result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'starting', 0, 1, 0, ?, 0, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    case_id,
                    idempotency_key,
                    request.company,
                    request.symbol,
                    request.market,
                    request.question,
                    request.agent,
                    request.depth,
                    _json(frontier),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO plans(id, run_id, version, plan_json, created_at) VALUES (?, ?, 1, ?, ?)",
                (plan_id, run_id, _json(plan), now),
            )
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, 0, 1, 1, ?, ?, ?)
                """,
                (checkpoint_id, run_id, _json(frontier), _json(state), now),
            )
            connection.execute(
                """
                INSERT INTO run_leases(run_id, owner_id, lease_token, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, owner_id, lease_token, lease_expires_at, now),
            )
            self._add_event(
                connection,
                run_id=run_id,
                kind="run.started",
                step="starting",
                status="running",
                progress=0,
                message="研究任务已创建",
                payload={"plan_version": 1},
            )
            run = self._get_task(connection, run_id)
            if run is None:
                raise KeyError(run_id)
            return run, lease_token, True

    def create_task(self, request: ResearchCreate) -> dict[str, Any]:
        # Compatibility helper for callers that have not moved to DurableRunner yet.
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        run, _token, _created = self.create_run_atomic(
            request,
            owner_id="legacy-inline-worker",
            idempotency_key=str(uuid4()),
            lease_token=str(uuid4()),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        return run

    def _get_task(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        task = dict(row)
        task["result"] = json.loads(task.pop("result_json")) if task["result_json"] else None
        task["frontier"] = json.loads(task.pop("frontier_json") or "{}")
        task["recovery_required"] = bool(task["recovery_required"])
        task["evidence"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT citation_number, title, publisher, url, source_type, excerpt, agent
                FROM evidence WHERE run_id = ? ORDER BY citation_number
                """,
                (run_id,),
            ).fetchall()
        ]
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._get_task(connection, task_id)

    def get_runtime_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = self._get_task(connection, run_id)
            if run is None:
                raise KeyError(run_id)
            plan_row = connection.execute(
                "SELECT * FROM plans WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            checkpoint_row = connection.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            lease_row = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            plan = dict(plan_row) if plan_row else None
            if plan:
                plan.update(json.loads(plan.pop("plan_json")))
            checkpoint = dict(checkpoint_row) if checkpoint_row else None
            if checkpoint:
                checkpoint["frontier"] = json.loads(checkpoint.pop("frontier_json"))
                checkpoint["state"] = json.loads(checkpoint.pop("state_json"))
            lease = dict(lease_row) if lease_row else None
            steps = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM run_steps WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                ).fetchall()
            ]
            tool_calls = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                ).fetchall()
            ]
            counts = {
                "steps": connection.execute(
                    "SELECT COUNT(*) FROM run_steps WHERE run_id = ?", (run_id,)
                ).fetchone()[0],
                "tool_calls": connection.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE run_id = ?", (run_id,)
                ).fetchone()[0],
                "checkpoints": connection.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE run_id = ?", (run_id,)
                ).fetchone()[0],
            }
        return {
            "run": run,
            "plan": plan,
            "checkpoint": checkpoint,
            "lease": lease,
            "events": self.list_events(run_id),
            "counts": counts,
            "steps": steps,
            "tool_calls": tool_calls,
        }

    def renew_lease(self, run_id: str, *, lease_token: str, expires_at: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE run_leases SET expires_at = ?, updated_at = ?
                WHERE run_id = ? AND lease_token = ? AND expires_at > ?
                """,
                (expires_at, now, run_id, lease_token, now),
            )
            if cursor.rowcount != 1:
                raise PermissionError("lease token mismatch or lease expired")
            row = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row)

    def take_over_expired_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_token: str,
        expires_at: str,
    ) -> tuple[dict[str, Any], str]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] in TERMINAL_STATUSES or run["status"] == "paused":
                raise ValueError(f"cannot take over run in {run['status']}")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if lease is not None and lease["expires_at"] > now:
                raise PermissionError("run still has an active lease")
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'resuming', state_version = state_version + 1,
                    recovery_required = 1, updated_at = ?
                WHERE id = ? AND state_version = ?
                """,
                (now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent lease takeover")
            connection.execute(
                """
                INSERT INTO run_leases(run_id, owner_id, lease_token, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    lease_token = excluded.lease_token,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (run_id, owner_id, lease_token, expires_at, now),
            )
            self._add_event(
                connection,
                run_id=run_id,
                kind="run.resuming",
                step=run["current_step"],
                status="resuming",
                progress=run["progress"],
                message="运行租约已过期，正在从检查点恢复",
                payload={"recovery_required": True},
            )
            updated = self._get_task(connection, run_id)
            if updated is None:
                raise KeyError(run_id)
            return updated, str(run["status"])

    def commit_step_atomic(
        self,
        run_id: str,
        *,
        lease_token: str,
        step_id: str,
        kind: str,
        step_input: dict[str, Any],
        step_output: dict[str, Any],
        idempotency_key: str,
        frontier: dict[str, Any],
        progress: int,
        budget_delta: int,
        tool: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not 0 <= progress < 100:
            raise ValueError("step progress must be between 0 and 99")
        if budget_delta < 0:
            raise ValueError("budget_delta cannot be negative")
        required_frontier_lists = {
            "ready_step_ids", "running_step_ids", "blocked_step_ids", "completed_step_ids"
        }
        if not all(isinstance(frontier.get(key, []), list) for key in required_frontier_lists):
            raise ValueError("frontier step collections must be lists")
        fingerprint = _commit_fingerprint({
            "step_id": step_id,
            "kind": kind,
            "step_input": step_input,
            "step_output": step_output,
            "frontier": frontier,
            "progress": progress,
            "budget_delta": budget_delta,
            "tool": tool,
        })
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] not in {"running", "pause_requested"}:
                raise ValueError(f"cannot commit a step in {run['status']}")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")
            existing = connection.execute(
                """
                SELECT id, kind, input_json, output_json, commit_fingerprint FROM run_steps
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["commit_fingerprint"] != fingerprint:
                    raise ValueError("step idempotency key was reused for a different operation")
                current = self._get_task(connection, run_id)
                if current is None:
                    raise KeyError(run_id)
                return current

            plan_version = int(frontier.get("plan_version") or 1)
            latest_plan_version = connection.execute(
                "SELECT MAX(version) FROM plans WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if plan_version != latest_plan_version:
                raise ValueError("frontier plan_version does not match the latest plan")
            if progress < int(run["progress"]):
                raise ValueError("step progress cannot move backwards")
            internal_step_id = f"{run_id}:{step_id}"
            cursor = connection.execute(
                """
                INSERT INTO run_steps(
                    id, run_id, plan_version, kind, status, input_json, output_json,
                    error, idempotency_key, commit_fingerprint, attempt, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'succeeded', ?, ?, NULL, ?, ?, 1, ?, ?)
                """,
                (
                    internal_step_id,
                    run_id,
                    plan_version,
                    kind,
                    _json(step_input),
                    _json(step_output),
                    idempotency_key,
                    fingerprint,
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent step commit")
            if tool is not None:
                connection.execute(
                    """
                    INSERT INTO tool_calls(
                        id, run_id, step_id, tool_name, tool_version, status,
                        input_json, output_json, error, duration_ms, cost_units,
                        idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run_id,
                        internal_step_id,
                        tool["name"],
                        tool.get("version", "1"),
                        _json(tool.get("input", {})),
                        _json(tool.get("output", {})),
                        tool.get("duration_ms"),
                        int(tool.get("cost_units", 0)),
                        tool["idempotency_key"],
                        now,
                        now,
                    ),
                )

            checkpoint_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            target_status = "paused" if run["status"] == "pause_requested" else "running"
            new_version = int(run["state_version"]) + 1
            new_budget = int(run["budget_used"]) + int(budget_delta)
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, current_step = ?, progress = ?, state_version = ?,
                    budget_used = ?, frontier_json = ?, updated_at = ?
                WHERE id = ? AND status = ? AND state_version = ?
                """,
                (
                    target_status,
                    step_id,
                    progress,
                    new_version,
                    new_budget,
                    _json(frontier),
                    now,
                    run_id,
                    run["status"],
                    run["state_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent step commit")
            state = {
                "plan_version": plan_version,
                "frontier": frontier,
                "budget_used": new_budget,
                "last_step_id": step_id,
            }
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    run_id,
                    checkpoint_sequence,
                    new_version,
                    plan_version,
                    _json(frontier),
                    _json(state),
                    now,
                ),
            )
            self._add_event(
                connection,
                run_id=run_id,
                kind="step.completed",
                step=step_id,
                status=target_status,
                progress=progress,
                message=f"步骤 {step_id} 已完成并保存检查点",
                payload={"checkpoint_sequence": checkpoint_sequence},
            )
            if target_status == "paused":
                connection.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
                self._add_event(
                    connection,
                    run_id=run_id,
                    kind="run.paused",
                    step=step_id,
                    status="paused",
                    progress=progress,
                    message="研究已在安全检查点暂停",
                )
            updated = self._get_task(connection, run_id)
            if updated is None:
                raise KeyError(run_id)
            return updated

    def complete_run_atomic(
        self,
        run_id: str,
        *,
        lease_token: str,
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] == "completed":
                current = self._get_task(connection, run_id)
                if current is None:
                    raise KeyError(run_id)
                return current
            if run["status"] != "running":
                raise ValueError(f"cannot complete run in {run['status']}")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")

            connection.execute("DELETE FROM evidence WHERE run_id = ?", (run_id,))
            connection.executemany(
                """
                INSERT INTO evidence (
                    id, run_id, citation_number, title, publisher, url,
                    source_type, excerpt, agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()), run_id, item["citation_number"], item["title"],
                        item["publisher"], _safe_public_url(item["url"]), item["source_type"],
                        item["excerpt"], item["agent"], now,
                    )
                    for item in evidence
                ],
            )
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            new_version = int(run["state_version"]) + 1
            frontier = json.loads(run["frontier_json"] or "{}")
            state = {
                "plan_version": int(frontier.get("plan_version") or 1),
                "frontier": frontier,
                "budget_used": int(run["budget_used"]),
                "report_committed": True,
            }
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), run_id, sequence, new_version,
                    state["plan_version"], _json(frontier), _json(state), now,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'completed', current_step = 'completed', progress = 100,
                    state_version = ?, result_json = ?, updated_at = ?, error = NULL
                WHERE id = ? AND status = 'running' AND state_version = ?
                """,
                (new_version, _json(result), now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent completion")
            self._add_event(
                connection,
                run_id=run_id,
                kind="report.completed",
                step="completed",
                status="completed",
                progress=100,
                message="研究报告与证据已提交",
                payload={"evidence_count": len(evidence)},
            )
            self._add_event(
                connection,
                run_id=run_id,
                kind="run.completed",
                step="completed",
                status="completed",
                progress=100,
                message="研究完成",
            )
            connection.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
            completed = self._get_task(connection, run_id)
            if completed is None:
                raise KeyError(run_id)
            return completed

    def append_runtime_event(
        self,
        run_id: str,
        *,
        kind: str,
        step: str,
        progress: int,
        message: str,
        payload: dict[str, Any] | None = None,
        lease_token: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")
            self._add_event(
                connection,
                run_id=run_id,
                kind=kind,
                step=step,
                status=run["status"],
                progress=progress,
                message=message,
                payload=payload,
            )

    def cas_transition(
        self,
        run_id: str,
        *,
        from_statuses: Iterable[str],
        to_status: str,
        kind: str,
        message: str,
        step: str | None = None,
        expected_version: int | None = None,
        lease_token: str | None = None,
        delete_lease: bool = False,
        new_lease: tuple[str, str, str] | None = None,
        error: str | None = None,
        clear_recovery_required: bool = False,
    ) -> dict[str, Any]:
        statuses = tuple(from_statuses)
        if not statuses:
            raise ValueError("from_statuses cannot be empty")
        if to_status not in SIX_RUN_STATES:
            raise ValueError(f"invalid run status: {to_status}")
        if any((status, to_status) not in LEGAL_TRANSITIONS for status in statuses):
            raise ValueError(f"illegal state edge to {to_status}")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] not in statuses:
                raise ValueError(f"illegal transition from {row['status']} to {to_status}")
            if expected_version is not None and row["state_version"] != expected_version:
                raise ValueError("stale state version")
            if lease_token is not None:
                lease = connection.execute(
                    "SELECT lease_token, expires_at FROM run_leases WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if (
                    lease is None
                    or lease["lease_token"] != lease_token
                    or lease["expires_at"] <= now
                ):
                    raise PermissionError("lease token mismatch or lease expired")

            current_step = step or row["current_step"]
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, current_step = ?, state_version = state_version + 1,
                    updated_at = ?, error = COALESCE(?, error),
                    recovery_required = CASE WHEN ? THEN 0 ELSE recovery_required END
                WHERE id = ? AND status = ? AND state_version = ?
                """,
                (
                    to_status,
                    current_step,
                    now,
                    error,
                    1 if clear_recovery_required else 0,
                    run_id,
                    row["status"],
                    row["state_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent state transition")
            if delete_lease:
                connection.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
            if new_lease is not None:
                owner_id, token, expires_at = new_lease
                connection.execute(
                    """
                    INSERT INTO run_leases(run_id, owner_id, lease_token, expires_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        lease_token = excluded.lease_token,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, owner_id, token, expires_at, now),
                )
            self._add_event(
                connection,
                run_id=run_id,
                kind=kind,
                step=current_step,
                status=to_status,
                progress=int(row["progress"]),
                message=message,
            )
            run = self._get_task(connection, run_id)
            if run is None:
                raise KeyError(run_id)
            return run

    def list_recovery_candidates(self) -> list[str]:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT runs.id
                FROM agent_runs AS runs
                LEFT JOIN run_leases AS leases ON leases.run_id = runs.id
                WHERE runs.status IN ('running', 'pause_requested', 'resuming')
                  AND (leases.run_id IS NULL OR leases.expires_at <= ?)
                ORDER BY runs.updated_at, runs.id
                """,
                (now,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def expire_owner_leases(self, owner_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE run_leases SET expires_at = ?, updated_at = ? WHERE owner_id = ?",
                ("1970-01-01T00:00:00+00:00", utc_now(), owner_id),
            )

    def update_task_identity(
        self,
        task_id: str,
        *,
        company: str,
        symbol: str | None,
        market: str,
        lease_token: str,
    ) -> dict[str, Any]:
        company = company.strip()
        market = market.strip().upper()
        if not company:
            raise ValueError("company cannot be empty")
        if market not in {"CN", "HK", "US", "OTHER"}:
            market = "OTHER"
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (task_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")
            run = connection.execute(
                "SELECT case_id FROM agent_runs WHERE id = ?", (task_id,)
            ).fetchone()
            if run is None:
                raise KeyError(task_id)
            connection.execute(
                "UPDATE agent_runs SET company = ?, symbol = ?, market = ?, updated_at = ? WHERE id = ?",
                (company, symbol or None, market, now, task_id),
            )
            connection.execute(
                "UPDATE cases SET company = ?, symbol = ?, market = ?, title = ?, updated_at = ? WHERE id = ?",
                (company, symbol or None, market, f"{company}公司研究", now, run["case_id"]),
            )
        resolved = self.get_task(task_id)
        if resolved is None:
            raise KeyError(task_id)
        return resolved

    def add_feedback(self, task_id: str, message: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        with self.connect() as connection:
            self._add_event(
                connection,
                run_id=task_id,
                kind="task.feedback",
                step=task["current_step"],
                status=task["status"],
                progress=task["progress"],
                message="已收到用户反馈",
                payload={"message": message},
            )
        result = self.get_task(task_id)
        if result is None:
            raise KeyError(task_id)
        return result

    def replace_evidence(
        self,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        lease_token: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status FROM agent_runs WHERE id = ?", (task_id,)
            ).fetchone()
            if run is None:
                raise KeyError(task_id)
            if lease_token is None:
                raise PermissionError("evidence replacement requires a lease token")
            else:
                lease = connection.execute(
                    "SELECT * FROM run_leases WHERE run_id = ?", (task_id,)
                ).fetchone()
                if (
                    lease is None
                    or lease["lease_token"] != lease_token
                    or lease["expires_at"] <= now
                ):
                    raise PermissionError("lease token mismatch or lease expired")
            connection.execute("DELETE FROM evidence WHERE run_id = ?", (task_id,))
            connection.executemany(
                """
                INSERT INTO evidence (
                    id, run_id, citation_number, title, publisher, url,
                    source_type, excerpt, agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()),
                        task_id,
                        item["citation_number"],
                        item["title"],
                        item["publisher"],
                        _safe_public_url(item["url"]),
                        item["source_type"],
                        item["excerpt"],
                        item["agent"],
                        now,
                    )
                    for item in items
                ],
            )

    def enrich_completed_evidence(
        self,
        run_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "completed":
                raise ValueError("evidence enrichment is only allowed for completed runs")
            evidence_fields = (
                "citation_number", "title", "publisher", "url",
                "source_type", "excerpt", "agent",
            )
            before_items = [
                {field: row[field] for field in evidence_fields}
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE run_id = ? ORDER BY citation_number",
                    (run_id,),
                ).fetchall()
            ]
            normalized_items = [
                {**item, "url": _safe_public_url(item["url"])} for item in items
            ]
            before_hash = _commit_fingerprint({"evidence": before_items})
            after_hash = _commit_fingerprint({"evidence": normalized_items})
            connection.execute("DELETE FROM evidence WHERE run_id = ?", (run_id,))
            connection.executemany(
                """
                INSERT INTO evidence (
                    id, run_id, citation_number, title, publisher, url,
                    source_type, excerpt, agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()), run_id, item["citation_number"], item["title"],
                        item["publisher"], _safe_public_url(item["url"]), item["source_type"],
                        item["excerpt"], item["agent"], now,
                    )
                    for item in normalized_items
                ],
            )
            latest = connection.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if latest is None:
                raise ValueError("completed run is missing its checkpoint")
            previous_state = json.loads(latest["state_json"])
            new_state = {
                **previous_state,
                "evidence_enriched": True,
                "evidence_count": len(normalized_items),
                "evidence_before_hash": before_hash,
                "evidence_after_hash": after_hash,
            }
            new_version = int(run["state_version"]) + 1
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), run_id, int(latest["sequence"]) + 1,
                    new_version, latest["plan_version"], latest["frontier_json"],
                    _json(new_state), now,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE agent_runs SET state_version = ?, updated_at = ?
                WHERE id = ? AND status = 'completed' AND state_version = ?
                """,
                (new_version, now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent evidence enrichment")
            self._add_event(
                connection,
                run_id=run_id,
                kind="evidence.enriched",
                step="completed",
                status="completed",
                progress=100,
                message="报告证据元数据已补充",
                payload={
                    "evidence_count": len(normalized_items),
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                },
            )
            enriched = self._get_task(connection, run_id)
            if enriched is None:
                raise KeyError(run_id)
            return enriched

    def get_completed_step_output(self, run_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT output_json FROM run_steps
                WHERE run_id = ? AND idempotency_key = ? AND status = 'succeeded'
                """,
                (run_id, idempotency_key),
            ).fetchone()
        return json.loads(row["output_json"]) if row and row["output_json"] else None

    def list_events(self, task_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id",
                (task_id, after_id),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["task_id"] = event["run_id"]
            event["payload"] = json.loads(event.pop("payload_json")) if event["payload_json"] else None
            events.append(event)
        return events

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return dict(row) if row else None

    def get_latest_task_for_case(
        self, case_id: str, *, statuses: Iterable[str] | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            self._require_case(connection, case_id)
            if statuses is None:
                row = connection.execute(
                    "SELECT id FROM agent_runs WHERE case_id = ? ORDER BY created_at DESC LIMIT 1",
                    (case_id,),
                ).fetchone()
            else:
                selected = tuple(statuses)
                if not selected:
                    return None
                placeholders = ",".join("?" for _ in selected)
                row = connection.execute(
                    f"""
                    SELECT id FROM agent_runs
                    WHERE case_id = ? AND status IN ({placeholders})
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (case_id, *selected),
                ).fetchone()
            return self._get_task(connection, row["id"]) if row else None

    def list_cases(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT cases.*, agent_runs.id AS latest_task_id,
                       agent_runs.status AS latest_status,
                       agent_runs.progress AS latest_progress
                FROM cases
                LEFT JOIN agent_runs ON agent_runs.id = (
                    SELECT id FROM agent_runs
                    WHERE case_id = cases.id ORDER BY created_at DESC LIMIT 1
                )
                ORDER BY cases.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _require_case(connection: sqlite3.Connection, case_id: str) -> None:
        if connection.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone() is None:
            raise KeyError(case_id)

    @staticmethod
    def _decode_turn(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["reason_codes"] = json.loads(item.pop("reason_codes_json") or "[]")
        return item

    def append_conversation_turn(
        self,
        case_id: str,
        *,
        turn_id: str,
        role: str,
        content: str,
        intent: str | None = None,
        reason_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid conversation role")
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("conversation content cannot be empty")
        codes = reason_codes or []
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            existing = connection.execute(
                "SELECT * FROM conversation_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["case_id"] != case_id
                    or existing["role"] != role
                    or existing["content"] != normalized_content
                    or existing["intent"] != intent
                    or json.loads(existing["reason_codes_json"] or "[]") != codes
                ):
                    raise ValueError("turn id was already used with different content")
                return self._decode_turn(existing)
            next_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM conversation_turns WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    id, case_id, sequence, role, content, intent, reason_codes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_id, case_id, next_sequence, role, normalized_content, intent, _json(codes), now),
            )
            connection.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
            row = connection.execute(
                "SELECT * FROM conversation_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            assert row is not None
            return self._decode_turn(row)

    def list_conversation_turns(self, case_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            self._require_case(connection, case_id)
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM conversation_turns WHERE case_id = ? ORDER BY sequence",
                    (case_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM conversation_turns
                        WHERE case_id = ? ORDER BY sequence DESC LIMIT ?
                    ) ORDER BY sequence
                    """,
                    (case_id, max(0, limit)),
                ).fetchall()
        return [self._decode_turn(row) for row in rows]

    def replace_case_summary(
        self, case_id: str, summary: str, *, last_turn_sequence: int
    ) -> dict[str, Any]:
        normalized = summary.strip()
        if not normalized:
            raise ValueError("case summary cannot be empty")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            latest_turn = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM conversation_turns WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            previous_cursor = int(
                connection.execute(
                    "SELECT COALESCE(MAX(last_turn_sequence), 0) FROM case_summaries WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            if (
                last_turn_sequence < 0
                or last_turn_sequence > latest_turn
                or last_turn_sequence < previous_cursor
            ):
                raise ValueError("last_turn_sequence must reference persisted turns monotonically")
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM case_summaries WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            summary_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_summaries(
                    id, case_id, version, summary, last_turn_sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (summary_id, case_id, version, normalized, last_turn_sequence, now),
            )
            row = connection.execute("SELECT * FROM case_summaries WHERE id = ?", (summary_id,)).fetchone()
            assert row is not None
            return dict(row)

    def get_case_summary(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            self._require_case(connection, case_id)
            row = connection.execute(
                "SELECT * FROM case_summaries WHERE case_id = ? ORDER BY version DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _decode_confirmation(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        raw_value = item.pop("resolved_value_json")
        item["resolved_value"] = json.loads(raw_value) if raw_value else None
        return item

    def put_pending_confirmation(
        self,
        case_id: str,
        *,
        confirmation_id: str,
        kind: str,
        prompt: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> dict[str, Any]:
        now = utc_now()
        canonical_expiry = _canonical_utc(expires_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            existing = connection.execute(
                "SELECT * FROM pending_confirmations WHERE id = ?", (confirmation_id,)
            ).fetchone()
            if existing is not None:
                decoded = self._decode_confirmation(existing)
                if (
                    decoded["case_id"] != case_id or decoded["kind"] != kind
                    or decoded["prompt"] != prompt or decoded["payload"] != payload
                    or decoded["expires_at"] != canonical_expiry
                ):
                    raise ValueError("confirmation id was already used with different content")
                return decoded
            connection.execute(
                """
                UPDATE pending_confirmations SET status = 'superseded', updated_at = ?
                WHERE case_id = ? AND status = 'pending'
                """,
                (now, case_id),
            )
            status = "pending" if canonical_expiry > now else "expired"
            connection.execute(
                """
                INSERT INTO pending_confirmations(
                    id, case_id, kind, prompt, payload_json, status, expires_at,
                    resolved_value_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    confirmation_id, case_id, kind, prompt.strip(), _json(payload),
                    status, canonical_expiry, now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pending_confirmations WHERE id = ?", (confirmation_id,)
            ).fetchone()
            assert row is not None
            return self._decode_confirmation(row)

    def get_pending_confirmation(self, case_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            connection.execute(
                """
                UPDATE pending_confirmations SET status = 'expired', updated_at = ?
                WHERE case_id = ? AND status = 'pending' AND expires_at <= ?
                """,
                (now, case_id, now),
            )
            row = connection.execute(
                """
                SELECT * FROM pending_confirmations
                WHERE case_id = ? AND status = 'pending' AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (case_id, now),
            ).fetchone()
        return self._decode_confirmation(row) if row else None

    def resolve_pending_confirmation(
        self, case_id: str, *, confirmation_id: str, value: dict[str, Any]
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            connection.execute(
                """
                UPDATE pending_confirmations SET status = 'expired', updated_at = ?
                WHERE id = ? AND case_id = ? AND status = 'pending' AND expires_at <= ?
                """,
                (now, confirmation_id, case_id, now),
            )
            cursor = connection.execute(
                """
                UPDATE pending_confirmations
                SET status = 'resolved', resolved_value_json = ?, updated_at = ?
                WHERE id = ? AND case_id = ? AND status = 'pending' AND expires_at > ?
                """,
                (_json(value), now, confirmation_id, case_id, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("confirmation is not pending")
            row = connection.execute(
                "SELECT * FROM pending_confirmations WHERE id = ?", (confirmation_id,)
            ).fetchone()
            assert row is not None
            return self._decode_confirmation(row)

    @staticmethod
    def _decode_route_request(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["decision"] = json.loads(item.pop("decision_json"))
        item["trace"] = json.loads(item.pop("trace_json"))
        return item

    def get_route_request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return self._decode_route_request(row) if row else None

    def save_route_request_result(
        self,
        request_id: str,
        *,
        case_id: str | None,
        message: str,
        decision: dict[str, Any],
        response: str,
        trace: list[str],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if case_id is not None:
                self._require_case(connection, case_id)
            existing = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                decoded = self._decode_route_request(existing)
                if decoded["case_id"] != case_id or decoded["message"] != message:
                    raise ValueError("request id was already used with a different route request")
                return decoded
            connection.execute(
                """
                INSERT INTO route_requests(
                    id, case_id, message, decision_json, response, trace_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, case_id, message, _json(decision), response, _json(trace), now),
            )
            row = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (request_id,)
            ).fetchone()
            assert row is not None
            return self._decode_route_request(row)

    @staticmethod
    def _decode_research_intake(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["candidates"] = json.loads(item.pop("candidates_json"))
        raw_entity = item.pop("resolved_entity_json")
        item["resolved_entity"] = json.loads(raw_entity) if raw_entity else None
        return item

    def create_research_intake(
        self,
        route_request_id: str,
        *,
        depth: str,
        budget_limit: int,
        resolution: dict[str, Any],
        confirmation_id: str | None = None,
        confirmation_expires_at: str | None = None,
    ) -> dict[str, Any]:
        if depth not in {"quick", "standard", "deep"}:
            raise ValueError("invalid research depth")
        if budget_limit <= 0:
            raise ValueError("budget_limit must be positive")
        resolution_status = resolution.get("status")
        candidates = list(resolution.get("candidates") or [])
        selected = resolution.get("selected")
        if resolution_status not in {"resolved", "ambiguous", "unresolved"}:
            raise ValueError("invalid entity resolution status")
        if resolution_status == "resolved" and not selected:
            raise ValueError("resolved intake requires a selected entity")
        if resolution_status == "ambiguous" and (
            len(candidates) < 2 or not confirmation_id or not confirmation_expires_at
        ):
            raise ValueError("ambiguous intake requires candidates and confirmation metadata")
        now = utc_now()
        canonical_expiry = (
            _canonical_utc(confirmation_expires_at) if confirmation_expires_at else None
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            route = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (route_request_id,)
            ).fetchone()
            if route is None:
                raise KeyError(route_request_id)
            decision = json.loads(route["decision_json"])
            if (
                decision.get("intent") not in {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"}
                or decision.get("requires_planner") is not True
                or decision.get("external_research_allowed") is not False
            ):
                raise PermissionError("route is not eligible for research intake")
            existing = connection.execute(
                "SELECT * FROM research_intakes WHERE route_request_id = ?",
                (route_request_id,),
            ).fetchone()
            if existing is not None:
                decoded = self._decode_research_intake(existing)
                if (
                    decoded["message"] != route["message"]
                    or decoded["depth"] != depth
                    or int(decoded["budget_limit"]) != int(budget_limit)
                ):
                    raise ValueError("route request was already used for a different intake")
                confirmation = connection.execute(
                    "SELECT id FROM entity_confirmations WHERE intake_id = ?",
                    (decoded["id"],),
                ).fetchone()
                decoded["confirmation_id"] = confirmation["id"] if confirmation else None
                return decoded

            intake_id = str(uuid4())
            status = {
                "resolved": "ready",
                "ambiguous": "awaiting_confirmation",
                "unresolved": "needs_clarification",
            }[str(resolution_status)]
            connection.execute(
                """
                INSERT INTO research_intakes(
                    id, route_request_id, message, depth, budget_limit, status,
                    entity_query, candidates_json, resolved_entity_json,
                    run_id, replan_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
                """,
                (
                    intake_id, route_request_id, route["message"], depth, budget_limit,
                    status, resolution.get("query"), _json(candidates),
                    _json(selected) if selected else None, now, now,
                ),
            )
            if status == "awaiting_confirmation":
                connection.execute(
                    """
                    INSERT INTO entity_confirmations(
                        id, intake_id, status, candidates_json, selected_candidate_id,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, NULL, ?, ?, ?)
                    """,
                    (confirmation_id, intake_id, _json(candidates), canonical_expiry, now, now),
                )
            row = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            assert row is not None
            decoded = self._decode_research_intake(row)
            decoded["confirmation_id"] = confirmation_id
            return decoded

    def get_research_intake(self, intake_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            confirmation = connection.execute(
                "SELECT * FROM entity_confirmations WHERE intake_id = ?", (intake_id,)
            ).fetchone()
            if (
                confirmation is not None
                and confirmation["status"] == "pending"
                and confirmation["expires_at"] <= now
            ):
                connection.execute(
                    "UPDATE entity_confirmations SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, confirmation["id"]),
                )
                connection.execute(
                    "UPDATE research_intakes SET status = 'needs_clarification', updated_at = ? WHERE id = ? AND status = 'awaiting_confirmation'",
                    (now, intake_id),
                )
            row = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            if row is None:
                return None
            decoded = self._decode_research_intake(row)
            latest_confirmation = connection.execute(
                "SELECT id FROM entity_confirmations WHERE intake_id = ?", (intake_id,)
            ).fetchone()
            decoded["confirmation_id"] = latest_confirmation["id"] if latest_confirmation else None
            return decoded

    def resolve_entity_confirmation(
        self,
        intake_id: str,
        *,
        candidate_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intake = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            if intake is None:
                raise KeyError(intake_id)
            confirmation = connection.execute(
                "SELECT * FROM entity_confirmations WHERE intake_id = ?", (intake_id,)
            ).fetchone()
            if confirmation is None:
                raise ValueError("intake has no entity confirmation")
            if confirmation["status"] == "resolved":
                if confirmation["selected_candidate_id"] != candidate_id:
                    raise ValueError("entity confirmation was already resolved differently")
                decoded = self._decode_research_intake(intake)
                decoded["confirmation_id"] = confirmation["id"]
                return decoded
            if confirmation["status"] != "pending" or confirmation["expires_at"] <= now:
                connection.execute(
                    "UPDATE entity_confirmations SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, confirmation["id"]),
                )
                connection.execute(
                    "UPDATE research_intakes SET status = 'needs_clarification', updated_at = ? WHERE id = ?",
                    (now, intake_id),
                )
                raise ValueError("entity confirmation expired")
            candidates = json.loads(confirmation["candidates_json"])
            selected = next(
                (item for item in candidates if item.get("candidate_id") == candidate_id),
                None,
            )
            if selected is None:
                raise ValueError("candidate does not belong to this confirmation")
            cursor = connection.execute(
                """
                UPDATE entity_confirmations
                SET status = 'resolved', selected_candidate_id = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (candidate_id, now, confirmation["id"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent entity confirmation")
            connection.execute(
                """
                UPDATE research_intakes
                SET status = 'ready', resolved_entity_json = ?, updated_at = ?
                WHERE id = ? AND status = 'awaiting_confirmation'
                """,
                (_json(selected), now, intake_id),
            )
            row = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            assert row is not None
            decoded = self._decode_research_intake(row)
            decoded["confirmation_id"] = confirmation["id"]
            return decoded

    @staticmethod
    def _add_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        step: str,
        status: str,
        progress: int,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                run_id, kind, step, status, progress, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind,
                step,
                status,
                progress,
                message,
                _json(payload) if payload is not None else None,
                utc_now(),
            ),
        )
