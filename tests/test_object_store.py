from datetime import datetime, timezone
import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.metadata import memberships, metadata, objects, tenants, users
from backend.object_store import MemoryObjectBackend, PrivateObjectService
from backend.object_store import S3ObjectBackend


def _service():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine); now = datetime.now(timezone.utc)
    with engine.begin() as c:
        for uid, tid in (("u1", "t1"), ("u2", "t2")):
            c.execute(users.insert().values(id=uid, email=f"{uid}@example.com", password_hash="x", created_at=now))
            c.execute(tenants.insert().values(id=tid, name=tid, created_at=now))
            c.execute(memberships.insert().values(tenant_id=tid, user_id=uid, role="owner"))
    backend = MemoryObjectBackend()
    return PrivateObjectService(engine, backend), backend


def test_pdf_upload_is_verified_promoted_and_reauthorized_for_download():
    service, backend = _service(); principal = PrincipalContext("u1", "t1", "owner")
    body = b"%PDF-1.7\nbody"
    slot = service.create_upload_slot(principal, declared_mime="application/pdf", declared_size=len(body))
    assert slot.upload_fields["Content-Type"] == "application/pdf"
    assert slot.upload_fields["max-bytes"] == str(len(body))
    backend.data[slot.upload_url.removeprefix("memory://put/")] = body
    ready = service.verify_and_promote(principal, slot.object_id)
    assert ready["status"] == "ready" and ready["sha256"]
    assert service.download_url(principal, slot.object_id).startswith("memory://get/private/t1/")
    with pytest.raises(KeyError):
        service.download_url(PrincipalContext("u2", "t2", "owner"), slot.object_id)


def test_spoofed_pdf_or_wrong_size_is_rejected_and_never_promoted():
    service, backend = _service(); principal = PrincipalContext("u1", "t1", "owner")
    body = b"not a pdf"
    slot = service.create_upload_slot(principal, declared_mime="application/pdf", declared_size=len(body))
    backend.data[slot.upload_url.removeprefix("memory://put/")] = body
    assert service.verify_and_promote(principal, slot.object_id)["status"] == "rejected"
    assert not any(key.startswith("private/") for key in backend.data)


def test_tombstone_then_delete_is_idempotent_and_cross_tenant_hidden():
    service, backend = _service(); principal = PrincipalContext("u1", "t1", "owner")
    body = b"plain text"
    slot = service.create_upload_slot(principal, declared_mime="text/plain", declared_size=len(body))
    backend.data[slot.upload_url.removeprefix("memory://put/")] = body
    service.verify_and_promote(principal, slot.object_id)
    assert service.tombstone(principal, slot.object_id)["status"] == "tombstoned"
    assert service.delete_tombstoned(principal, slot.object_id)["status"] == "deleted"
    assert service.delete_tombstoned(principal, slot.object_id)["status"] == "deleted"
    with pytest.raises(KeyError):
        service.tombstone(PrincipalContext("u2", "t2", "owner"), slot.object_id)


def test_concurrent_promotion_loser_cannot_delete_winners_final_bytes(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'objects.db'}", connect_args={"check_same_thread": False},
    )
    metadata.create_all(engine); now = datetime.now(timezone.utc)
    with engine.begin() as c:
        c.execute(users.insert().values(id="u1", email="u1@example.com", password_hash="x", created_at=now))
        c.execute(tenants.insert().values(id="t1", name="t1", created_at=now))
        c.execute(memberships.insert().values(tenant_id="t1", user_id="u1", role="owner"))

    class RacingBackend(MemoryObjectBackend):
        def __init__(self):
            super().__init__(); self.barrier = threading.Barrier(2); self.targets = []
        def copy(self, source, target):
            self.targets.append(target)
            self.barrier.wait(timeout=5)
            super().copy(source, target)

    backend = RacingBackend(); service = PrivateObjectService(engine, backend)
    principal = PrincipalContext("u1", "t1", "owner"); body = b"race-safe text"
    slot = service.create_upload_slot(principal, declared_mime="text/plain", declared_size=len(body))
    backend.data[slot.upload_url.removeprefix("memory://put/")] = body
    outcomes = []
    def promote():
        try: outcomes.append(service.verify_and_promote(principal, slot.object_id)["status"])
        except RuntimeError: outcomes.append("fenced")
    threads = [threading.Thread(target=promote) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert sorted(outcomes) == ["fenced", "ready"] and len(set(backend.targets)) == 2
    with engine.connect() as c:
        winner_key = c.scalar(select(objects.c.object_key).where(objects.c.id == slot.object_id))
    assert winner_key in backend.data and backend.data[winner_key] == body


def test_download_presign_uses_public_client_not_internal_minio_client():
    class Client:
        def __init__(self, name): self.name = name
        def generate_presigned_url(self, *_args, **_kwargs): return f"https://{self.name}/download"
    backend = S3ObjectBackend(Client("internal"), bucket="b", presign_client=Client("public"))
    assert backend.presign_get("private/key", expires_seconds=60) == "https://public/download"
