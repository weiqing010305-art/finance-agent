from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import Repository
from backend.memory import MemoryService
from backend.schemas import MemoryCandidate, MemoryScope


def test_preference_list_delete_and_deletion_status(tmp_path):
    with TestClient(create_app(tmp_path / "api-memory.db", mock_delay=0, load_env_file=False)) as client:
        created = client.post("/api/memory/preferences", json={
            "memory_key": "report_style", "value": {"style": "concise"},
            "text": "Prefer concise reports", "idempotency_key": "api-pref-1",
        })
        assert created.status_code == 200
        memory = created.json()
        assert [item["memory_id"] for item in client.get("/api/memory").json()] == [memory["memory_id"]]
        deleted = client.request("DELETE", f"/api/memory/{memory['memory_id']}", json={
            "idempotency_key": "api-delete-1"
        })
        assert deleted.status_code == 200
        assert client.get("/api/memory").json() == []
        job_id = deleted.json()["id"]
        assert client.post(f"/api/memory/deletions/{job_id}/process").json()["status"] == "completed"
        assert client.get(f"/api/memory/deletions/{job_id}").json()["status"] == "completed"


def test_preference_idempotency_conflict_is_409(tmp_path):
    with TestClient(create_app(tmp_path / "api-memory-conflict.db", mock_delay=0, load_env_file=False)) as client:
        payload = {"memory_key": "style", "value": {"style": "brief"},
                   "text": "Use brief style", "idempotency_key": "same"}
        assert client.post("/api/memory/preferences", json=payload).status_code == 200
        payload["text"] = "Use detailed style"
        assert client.post("/api/memory/preferences", json=payload).status_code == 409


def test_deletion_job_endpoint_hides_other_principal(tmp_path):
    path = tmp_path / "api-memory-other.db"
    repo = Repository(path); repo.initialize()
    other = MemoryService(repo).remember(MemoryCandidate(
        memory_type="user_preference", memory_key="private",
        scope=MemoryScope(scope_kind="user", tenant_id="other", user_id="bob"),
        content={"secret": True}, content_text="PRIVATE-BODY",
        idempotency_key="other-memory", confidence=1, explicit_user_confirmation=True,
    ))
    job = repo.tombstone_memory_atomic(
        other.memory_id, tenant_id="other", user_id="bob", idempotency_key="other-delete"
    )
    with TestClient(create_app(path, mock_delay=0, load_env_file=False)) as client:
        assert client.get(f"/api/memory/deletions/{job['id']}").status_code == 404
        assert client.post(f"/api/memory/deletions/{job['id']}/process").status_code == 404
