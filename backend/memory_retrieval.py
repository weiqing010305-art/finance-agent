from __future__ import annotations

from dataclasses import dataclass

from backend.database import Repository
from backend.memory import scope_hash
from backend.schemas import MemoryContextItem, MemoryScope


@dataclass(frozen=True)
class MemoryPrincipal:
    tenant_id: str
    user_id: str


class MemoryRetriever:
    def __init__(self, repository: Repository, *, max_items: int = 8, max_chars: int = 2_000):
        self.repository = repository
        self.max_items = max_items
        self.max_chars = max_chars

    def retrieve(
        self, *, principal: MemoryPrincipal, case_id: str | None = None,
        company: str | None = None, symbol: str | None = None, market: str | None = None,
        now: str | None = None,
    ) -> list[MemoryContextItem]:
        scopes = [MemoryScope(
            scope_kind="user", tenant_id=principal.tenant_id, user_id=principal.user_id,
        )]
        if case_id:
            scopes.append(MemoryScope(
                scope_kind="case", tenant_id=principal.tenant_id,
                user_id=principal.user_id, case_id=case_id,
                company=company, symbol=symbol, market=market,
            ))
        if company and market:
            scopes.append(MemoryScope(
                scope_kind="public_company", tenant_id="public", company=company,
                symbol=symbol, market=market,
            ))
        views = self.repository.query_active_memories(
            scope_hashes=[scope_hash(item) for item in scopes], now=now, limit=32,
        )
        selected: list[MemoryContextItem] = []
        used = 0
        for view in views:
            if len(selected) >= self.max_items:
                break
            remaining = self.max_chars - used
            if remaining <= 0:
                break
            text = view.content_text[:remaining]
            if not text:
                continue
            selected.append(MemoryContextItem(
                memory_id=view.memory_id, memory_type=view.memory_type,
                content_text=text, confidence=view.confidence,
                evidence_ids=view.evidence_ids, expires_at=view.expires_at,
            ))
            used += len(text)
        return selected
