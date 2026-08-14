from __future__ import annotations

import os
import time

from sqlalchemy import create_engine

from backend.jobs.dispatch import GlobalOutboxDispatcher
from backend.jobs.ledger import JobLedger
from backend.jobs.worker import execute_job
from backend.settings import RuntimeSettings


def build_dispatcher() -> GlobalOutboxDispatcher:
    settings = RuntimeSettings.from_env()
    if settings.mode != "local":
        raise RuntimeError("global dispatcher is formal-runtime only")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return GlobalOutboxDispatcher(
        engine, JobLedger(engine), sender=lambda job_id: execute_job.send(job_id),
    )


def main() -> None:
    interval = max(1.0, min(float(os.getenv("JOB_DISPATCH_INTERVAL_SECONDS", "5")), 60.0))
    dispatcher = build_dispatcher()
    while True:
        try:
            dispatcher.publish_due(limit=100)
        except Exception as exc:
            # Never include persisted payloads or credentials in dispatcher logs.
            print(f"job dispatcher cycle failed: {type(exc).__name__}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
