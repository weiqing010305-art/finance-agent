from __future__ import annotations

from backend.database import Repository


class MemoryMaintenance:
    def __init__(self, repository: Repository):
        self.repository = repository

    def expire(self, *, now: str | None = None) -> int:
        return self.repository.expire_memory_versions(now=now)

    def delete(self, job_id: str) -> dict:
        token = self.repository.claim_memory_deletion_job(job_id)
        return self.repository.finish_memory_deletion_job(job_id, claim_token=token)
