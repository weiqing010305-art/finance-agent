from __future__ import annotations

import json
import tempfile
from pathlib import Path

from backend.database import Repository
from backend.memory import MemoryService
from backend.memory_retrieval import MemoryPrincipal, MemoryRetriever
from backend.schemas import MemoryCandidate, MemoryScope


def run() -> dict:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        repo = Repository(Path(directory) / "phase5-eval.db")
        repo.initialize()
        service = MemoryService(repo)
        mine = service.remember(MemoryCandidate(
            memory_type="user_preference", memory_key="style",
            scope=MemoryScope(scope_kind="user", tenant_id="a", user_id="alice"),
            content={"style": "concise"}, content_text="Prefer concise reports",
            idempotency_key="eval-mine", confidence=1, explicit_user_confirmation=True,
        ))
        service.remember(MemoryCandidate(
            memory_type="user_preference", memory_key="style",
            scope=MemoryScope(scope_kind="user", tenant_id="b", user_id="bob"),
            content={"style": "secret"}, content_text="SECRET",
            idempotency_key="eval-other", confidence=1, explicit_user_confirmation=True,
        ))
        retrieved = MemoryRetriever(repo).retrieve(
            principal=MemoryPrincipal(tenant_id="a", user_id="alice")
        )
        return {
            "scope_leakage_rate": 0.0 if [item.memory_id for item in retrieved] == [mine.memory_id] else 1.0,
            "retrieval_precision_smoke": 1.0 if len(retrieved) == 1 else 0.0,
            "token_budget_pass": sum(len(item.content_text) for item in retrieved) <= 2000,
            "mode": "sqlite_offline_smoke",
        }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
