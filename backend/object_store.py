from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO, Protocol
from uuid import uuid4

from sqlalchemy import Engine, and_, insert, select, update

from backend.auth.models import PrincipalContext
from backend.db.metadata import objects
from backend.db.session import principal_transaction


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ObjectBackend(Protocol):
    def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_seconds: int,
    ) -> tuple[str, dict[str, str]]: ...
    def presign_get(self, key: str, *, expires_seconds: int) -> str: ...
    def open(self, key: str) -> BinaryIO: ...
    def copy(self, source: str, target: str) -> None: ...
    def delete(self, key: str) -> None: ...


class S3ObjectBackend:
    def __init__(self, client, *, bucket: str, presign_client=None):
        self.client, self.presign_client, self.bucket = client, presign_client or client, bucket

    def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_seconds: int,
    ) -> tuple[str, dict[str, str]]:
        result = self.presign_client.generate_presigned_post(
            Bucket=self.bucket, Key=key, Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
            ExpiresIn=expires_seconds,
        )
        return result["url"], result["fields"]

    def presign_get(self, key: str, *, expires_seconds: int) -> str:
        return self.presign_client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_seconds,
        )

    def open(self, key: str) -> BinaryIO:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def copy(self, source: str, target: str) -> None:
        self.client.copy_object(Bucket=self.bucket, Key=target, CopySource={"Bucket": self.bucket, "Key": source})

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


@dataclass(frozen=True)
class UploadSlot:
    object_id: str
    upload_url: str
    upload_fields: dict[str, str]
    expires_in: int


def detect_mime(prefix: bytes) -> str:
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    try:
        prefix.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


class PrivateObjectService:
    def __init__(self, engine: Engine, backend: ObjectBackend, *, max_bytes: int = 25 * 1024 * 1024):
        self.engine, self.backend, self.max_bytes = engine, backend, max_bytes

    def create_upload_slot(
        self, principal: PrincipalContext, *, declared_mime: str, declared_size: int,
    ) -> UploadSlot:
        if declared_mime not in {"application/pdf", "text/plain"}:
            raise ValueError("unsupported content type")
        if declared_size <= 0 or declared_size > self.max_bytes:
            raise ValueError("invalid object size")
        object_id, now = str(uuid4()), _now()
        quarantine_key = f"quarantine/{uuid4().hex}"
        with principal_transaction(self.engine, principal) as connection:
            connection.execute(insert(objects).values(
                id=object_id, tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
                status="pending", quarantine_key=quarantine_key, declared_mime=declared_mime,
                declared_size=declared_size, created_at=now, updated_at=now,
            ))
        upload_url, upload_fields = self.backend.presign_upload(
            quarantine_key, content_type=declared_mime,
            max_bytes=min(declared_size, self.max_bytes), expires_seconds=600,
        )
        return UploadSlot(object_id, upload_url, upload_fields, 600)

    def verify_and_promote(self, principal: PrincipalContext, object_id: str) -> dict:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(objects).where(and_(
                objects.c.id == object_id, objects.c.status == "pending",
                objects.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
        if row is None:
            raise KeyError("object not found")
        digest, size, prefix = hashlib.sha256(), 0, b""
        with self.backend.open(row["quarantine_key"]) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                if not prefix:
                    prefix = chunk[:4096]
                size += len(chunk)
                if size > self.max_bytes:
                    return self._reject(principal, row, "object exceeds maximum size")
                digest.update(chunk)
        verified_mime = detect_mime(prefix)
        if size != row["declared_size"] or verified_mime != row["declared_mime"]:
            return self._reject(principal, row, "object metadata mismatch")
        sha256 = digest.hexdigest()
        final_key = f"private/{principal.tenant_id}/{object_id}/{sha256}/{uuid4().hex}"
        self.backend.copy(row["quarantine_key"], final_key)
        now = _now()
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(objects).where(and_(
                objects.c.id == object_id, objects.c.status == "pending",
                objects.c.tenant_id == principal.tenant_id,
            )).values(
                status="ready", object_key=final_key, verified_mime=verified_mime,
                verified_size=size, sha256=sha256, updated_at=now,
            ))
            if result.rowcount != 1:
                self.backend.delete(final_key)
                raise RuntimeError("object state changed during promotion")
        # Cleanup is deliberately after the authoritative commit. A crash leaves
        # an extra quarantine object, never a ready row pointing at missing bytes.
        self.backend.delete(row["quarantine_key"])
        return self.get(principal, object_id)

    def get(self, principal: PrincipalContext, object_id: str) -> dict:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(objects).where(and_(
                objects.c.id == object_id, objects.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
        if row is None:
            raise KeyError("object not found")
        return {key: row[key] for key in (
            "id", "tenant_id", "status", "verified_mime", "verified_size", "sha256",
        )}

    def download_url(self, principal: PrincipalContext, object_id: str) -> str:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(objects.c.object_key).where(and_(
                objects.c.id == object_id, objects.c.status == "ready",
                objects.c.tenant_id == principal.tenant_id,
            ))).one_or_none()
        if row is None:
            raise KeyError("object not found")
        return self.backend.presign_get(row.object_key, expires_seconds=300)

    def tombstone(self, principal: PrincipalContext, object_id: str) -> dict:
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(objects).where(and_(
                objects.c.id == object_id, objects.c.tenant_id == principal.tenant_id,
                objects.c.status.in_(("pending", "ready", "rejected")),
            )).values(status="tombstoned", updated_at=_now()))
            if result.rowcount != 1:
                existing = connection.execute(select(objects.c.status).where(and_(
                    objects.c.id == object_id, objects.c.tenant_id == principal.tenant_id,
                ))).scalar_one_or_none()
                if existing not in {"tombstoned", "deleted"}:
                    raise KeyError("object not found")
        return self.get(principal, object_id)

    def delete_tombstoned(self, principal: PrincipalContext, object_id: str) -> dict:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(
                objects.c.quarantine_key, objects.c.object_key, objects.c.status,
            ).where(and_(objects.c.id == object_id, objects.c.tenant_id == principal.tenant_id))).one_or_none()
        if row is None:
            raise KeyError("object not found")
        if row.status == "deleted":
            return self.get(principal, object_id)
        if row.status != "tombstoned":
            raise ValueError("object is not tombstoned")
        self.backend.delete(row.quarantine_key)
        if row.object_key:
            self.backend.delete(row.object_key)
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(objects).where(and_(
                objects.c.id == object_id, objects.c.tenant_id == principal.tenant_id,
                objects.c.status == "tombstoned",
            )).values(status="deleted", deleted_at=_now(), updated_at=_now()))
            if result.rowcount != 1:
                raise RuntimeError("object deletion fencing conflict")
        return self.get(principal, object_id)

    def _reject(self, principal: PrincipalContext, row, reason: str) -> dict:
        with principal_transaction(self.engine, principal) as connection:
            connection.execute(update(objects).where(and_(
                objects.c.id == row["id"], objects.c.status == "pending",
                objects.c.tenant_id == principal.tenant_id,
            )).values(status="rejected", updated_at=_now()))
        self.backend.delete(row["quarantine_key"])
        result = self.get(principal, row["id"])
        result["reason"] = reason
        return result


class MemoryObjectBackend:
    """Deterministic test adapter; production uses S3ObjectBackend/MinIO."""
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_seconds: int,
    ) -> tuple[str, dict[str, str]]:
        return f"memory://put/{key}", {
            "Content-Type": content_type, "key": key, "max-bytes": str(max_bytes),
        }

    def presign_get(self, key: str, *, expires_seconds: int) -> str:
        return f"memory://get/{key}"

    def open(self, key: str) -> BinaryIO:
        return io.BytesIO(self.data[key])

    def copy(self, source: str, target: str) -> None:
        self.data[target] = self.data[source]

    def delete(self, key: str) -> None:
        self.data.pop(key, None)
