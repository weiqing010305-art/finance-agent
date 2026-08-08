from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.schemas import ResearchCreate


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            connection.executescript(
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

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    company TEXT NOT NULL,
                    symbol TEXT,
                    market TEXT NOT NULL,
                    question TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    depth TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
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
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    citation_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, citation_number)
                );

                CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_tasks_case_id ON tasks(case_id, created_at DESC);
                """
            )

    def create_task(self, request: ResearchCreate) -> dict[str, Any]:
        now = utc_now()
        task_id = str(uuid4())
        case_id = str(uuid4())
        title = f"{request.company}公司研究"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)",
                (case_id, request.company, request.symbol, request.market, title, now, now),
            )
            connection.execute(
                """
                INSERT INTO tasks (
                    id, case_id, company, symbol, market, question, agent, depth,
                    status, current_step, progress, created_at, updated_at, result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    task_id,
                    case_id,
                    request.company,
                    request.symbol,
                    request.market,
                    request.question,
                    request.agent,
                    request.depth,
                    "queued",
                    "queued",
                    0,
                    now,
                    now,
                ),
            )
            self._add_event(
                connection,
                task_id=task_id,
                kind="task.created",
                step="queued",
                status="queued",
                progress=0,
                message="研究任务已创建",
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            task["result"] = json.loads(task.pop("result_json")) if task["result_json"] else None
            task["evidence"] = [dict(item) for item in connection.execute(
                "SELECT citation_number, title, publisher, url, source_type, excerpt, agent FROM evidence WHERE task_id = ? ORDER BY citation_number",
                (task_id,),
            ).fetchall()]
            return task

    def update_task(
        self,
        task_id: str,
        *,
        status: str,
        step: str,
        progress: int,
        message: str,
        kind: str = "task.progress",
        result: dict[str, Any] | None = None,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, current_step = ?, progress = ?, updated_at = ?,
                    result_json = COALESCE(?, result_json), error = ?
                WHERE id = ?
                """,
                (
                    status,
                    step,
                    progress,
                    now,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    task_id,
                ),
            )
            if connection.total_changes == 0:
                raise KeyError(task_id)
            self._add_event(
                connection,
                task_id=task_id,
                kind=kind,
                step=step,
                status=status,
                progress=progress,
                message=message,
                payload=payload,
            )
            connection.execute(
                "UPDATE cases SET updated_at = ? WHERE id = (SELECT case_id FROM tasks WHERE id = ?)",
                (now, task_id),
            )
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def update_task_identity(
        self,
        task_id: str,
        *,
        company: str,
        symbol: str | None,
        market: str,
    ) -> dict[str, Any]:
        company = company.strip()
        market = market.strip().upper()
        if not company:
            raise ValueError("company cannot be empty")
        if market not in {"CN", "HK", "US", "OTHER"}:
            market = "OTHER"
        now = utc_now()
        with self.connect() as connection:
            task = connection.execute(
                "SELECT case_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            connection.execute(
                "UPDATE tasks SET company = ?, symbol = ?, market = ?, updated_at = ? WHERE id = ?",
                (company, symbol or None, market, now, task_id),
            )
            connection.execute(
                "UPDATE cases SET company = ?, symbol = ?, market = ?, title = ?, updated_at = ? WHERE id = ?",
                (company, symbol or None, market, f"{company}公司研究", now, task["case_id"]),
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
                task_id=task_id,
                kind="task.feedback",
                step=task["current_step"],
                status=task["status"],
                progress=task["progress"],
                message="已收到用户反馈",
                payload={"message": message},
            )
        return self.get_task(task_id)

    def replace_evidence(self, task_id: str, items: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("DELETE FROM evidence WHERE task_id = ?", (task_id,))
            connection.executemany(
                """
                INSERT INTO evidence (
                    id, task_id, citation_number, title, publisher, url,
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
                        item["url"],
                        item["source_type"],
                        item["excerpt"],
                        item["agent"],
                        now,
                    )
                    for item in items
                ],
            )

    def list_events(self, task_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id",
                (task_id, after_id),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json")) if event["payload_json"] else None
            events.append(event)
        return events

    def list_cases(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT cases.*, tasks.id AS latest_task_id, tasks.status AS latest_status,
                       tasks.progress AS latest_progress
                FROM cases
                LEFT JOIN tasks ON tasks.id = (
                    SELECT id FROM tasks WHERE case_id = cases.id ORDER BY created_at DESC LIMIT 1
                )
                ORDER BY cases.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _add_event(
        connection: sqlite3.Connection,
        *,
        task_id: str,
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
                task_id, kind, step, status, progress, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                kind,
                step,
                status,
                progress,
                message,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                utc_now(),
            ),
        )
