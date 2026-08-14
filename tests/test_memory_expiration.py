from datetime import datetime, timedelta, timezone

from backend.database import Repository
from backend.memory import MemoryService, MemoryTTLPolicy
from backend.memory_jobs import MemoryMaintenance
from backend.schemas import MemoryCandidate, MemoryScope


def test_expired_memory_is_auditable_but_not_retrievable(tmp_path):
    repo = Repository(tmp_path / "expiry.db"); repo.initialize()
    service = MemoryService(repo, ttl_policy=MemoryTTLPolicy(entity_identity_days=180))
    # Preference has no TTL; create a directly expiring active version for boundary testing.
    view = service.remember(MemoryCandidate(
        memory_type="user_preference", memory_key="format",
        scope=MemoryScope(scope_kind="user", tenant_id="local", user_id="default"),
        content={"format": "brief"}, content_text="Use brief format",
        idempotency_key="expiry-pref", confidence=1, explicit_user_confirmation=True,
    ))
    boundary = datetime.now(timezone.utc)
    with repo.connect() as connection:
        connection.execute("UPDATE memory_versions SET expires_at=? WHERE id=?", (boundary.isoformat(), view.id))
    assert MemoryMaintenance(repo).expire(now=boundary.isoformat()) == 1
    assert repo.list_memory_versions(view.memory_id)[0].status == "expired"
    assert repo.query_active_memories(
        scope_hashes=[__import__('backend.memory', fromlist=['scope_hash']).scope_hash(view.scope)],
        now=(boundary + timedelta(seconds=1)).isoformat(),
    ) == []
