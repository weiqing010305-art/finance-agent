import pytest

from backend.database import Repository
from backend.memory import MemoryService, scope_hash
from backend.schemas import MemoryCandidate, MemoryScope


def _private(repo, key="delete-me"):
    return MemoryService(repo).remember(MemoryCandidate(
        memory_type="user_preference", memory_key="style",
        scope=MemoryScope(scope_kind="user", tenant_id="local", user_id="default"),
        content={"style": "concise"}, content_text="Prefer concise reports",
        idempotency_key=key, confidence=1, explicit_user_confirmation=True,
    ))


def test_tombstone_is_immediately_unreadable_and_job_is_fenced(tmp_path):
    repo = Repository(tmp_path / "delete.db"); repo.initialize()
    view = _private(repo)
    job = repo.tombstone_memory_atomic(
        view.memory_id, tenant_id="local", user_id="default", idempotency_key="del-1"
    )
    assert repo.query_active_memories(scope_hashes=[scope_hash(view.scope)]) == []
    token = repo.claim_memory_deletion_job(job["id"], ttl_seconds=300)
    with pytest.raises(ValueError, match="claim was lost"):
        repo.finish_memory_deletion_job(job["id"], claim_token="old-token")
    assert repo.finish_memory_deletion_job(job["id"], claim_token=token)["status"] == "completed"
    with repo.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_versions WHERE memory_id=?", (view.memory_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_events WHERE memory_id=?", (view.memory_id,)
        ).fetchone()[0] > 0


def test_delete_is_scope_protected_and_idempotent(tmp_path):
    repo = Repository(tmp_path / "delete-scope.db"); repo.initialize()
    view = _private(repo)
    with pytest.raises(PermissionError):
        repo.tombstone_memory_atomic(
            view.memory_id, tenant_id="other", user_id="attacker", idempotency_key="del-x"
        )
    first = repo.tombstone_memory_atomic(
        view.memory_id, tenant_id="local", user_id="default", idempotency_key="del-ok"
    )
    assert repo.tombstone_memory_atomic(
        view.memory_id, tenant_id="local", user_id="default", idempotency_key="del-ok"
    )["id"] == first["id"]
