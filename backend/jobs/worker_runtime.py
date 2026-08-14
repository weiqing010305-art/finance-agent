from __future__ import annotations

from collections.abc import Callable


_executor: Callable[[str], None] | None = None


def configure_executor(executor: Callable[[str], None]) -> None:
    global _executor
    _executor = executor


def execute_persisted_job(job_id: str) -> None:
    if _executor is None:
        raise RuntimeError("worker PostgreSQL executor is not configured")
    _executor(job_id)
