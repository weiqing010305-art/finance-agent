from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.database import Repository
from backend.schemas import MemoryCandidate, MemoryScope, MemoryType, MemoryView


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(candidate: MemoryCandidate) -> str:
    return hashlib.sha256(
        canonical_json({"content": candidate.content, "text": candidate.content_text}).encode("utf-8")
    ).hexdigest()


def request_fingerprint(candidate: MemoryCandidate) -> str:
    identity = candidate.model_dump(mode="json")
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def scope_hash(scope: MemoryScope) -> str:
    identity = {
        "scope_kind": scope.scope_kind,
        "tenant_id": scope.tenant_id,
        "user_id": scope.user_id or "",
        "case_id": scope.case_id or "",
        "company": (scope.company or "").strip().casefold(),
        "symbol": (scope.symbol or "").strip().upper(),
        "market": (scope.market or "").strip().upper(),
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryTTLPolicy:
    company_fact_days: int = 90
    entity_identity_days: int = 180
    case_summary_days: int = 30
    task_experience_days: int = 90
    candidate_days: int = 7

    def expires_at(self, memory_type: MemoryType, *, now: datetime | None = None) -> str | None:
        current = now or datetime.now(timezone.utc)
        days = {
            "company_fact": self.company_fact_days,
            "entity_identity": self.entity_identity_days,
            "case_summary": self.case_summary_days,
            "task_experience": self.task_experience_days,
            "user_preference": None,
        }[memory_type]
        return None if days is None else (current + timedelta(days=days)).isoformat()

    def conflict_expires_at(self, *, now: datetime | None = None) -> str:
        current = now or datetime.now(timezone.utc)
        return (current + timedelta(days=self.candidate_days)).isoformat()


class MemoryService:
    def __init__(self, repository: Repository, *, ttl_policy: MemoryTTLPolicy | None = None):
        self.repository = repository
        self.ttl_policy = ttl_policy or MemoryTTLPolicy()

    def remember(self, candidate: MemoryCandidate) -> MemoryView:
        return self.repository.persist_memory_candidate_atomic(
            candidate,
            scope_digest=scope_hash(candidate.scope),
            content_digest=content_hash(candidate),
            fingerprint=request_fingerprint(candidate),
            expires_at=self.ttl_policy.expires_at(candidate.memory_type),
            conflict_expires_at=self.ttl_policy.conflict_expires_at(),
        )
