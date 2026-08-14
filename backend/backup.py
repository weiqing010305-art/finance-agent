from __future__ import annotations

import base64
import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"FINSCOPE-BACKUP-V1\0"
BUNDLE_MANIFEST = "finscope-manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_backup_key(path: str | Path) -> bytes:
    raw = Path(path).read_bytes().strip()
    if len(raw) == 32:
        key = raw
    else:
        try:
            key = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        except Exception:
            key = raw
    if len(key) != 32:
        raise ValueError("backup encryption key must be exactly 32 bytes")
    return key


@dataclass(frozen=True)
class BackupManifest:
    backup_type: str
    schema_revision: str
    parent_backup: str | None
    embedding_profile_id: str
    milvus_collection: str
    files: dict[str, dict[str, int | str]]
    created_at: str


class EncryptedBackupBundle:
    @staticmethod
    def create(
        source_dir: Path, destination: Path, *, key: bytes, backup_type: str,
        schema_revision: str, parent_backup: str | None = None,
        embedding_profile_id: str = "bge-large-zh-v1.5",
        milvus_collection: str = "finance_agent_chunks_v1",
    ) -> BackupManifest:
        if backup_type not in {"full", "incremental"} or len(key) != 32:
            raise ValueError("invalid backup configuration")
        if (source_dir / BUNDLE_MANIFEST).exists():
            raise ValueError(f"backup source contains reserved file {BUNDLE_MANIFEST}")
        files: dict[str, dict[str, int | str]] = {}
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                if path.is_symlink():
                    raise ValueError("backup sources cannot contain symlinks")
                relative = path.relative_to(source_dir).as_posix()
                files[relative] = {"sha256": _sha256(path), "size": path.stat().st_size}
        manifest = BackupManifest(
            backup_type=backup_type, schema_revision=schema_revision,
            parent_backup=parent_backup, embedding_profile_id=embedding_profile_id,
            milvus_collection=milvus_collection, files=files,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with tempfile.TemporaryDirectory(prefix="finscope-backup-") as temporary:
            tar_path = Path(temporary) / "payload.tar"
            manifest_path = Path(temporary) / BUNDLE_MANIFEST
            manifest_path.write_text(json.dumps(manifest.__dict__, sort_keys=True), encoding="utf-8")
            with tarfile.open(tar_path, "w") as archive:
                for path in sorted(source_dir.rglob("*")):
                    if path.is_file():
                        archive.add(path, arcname=path.relative_to(source_dir).as_posix(), recursive=False)
                archive.add(manifest_path, arcname=BUNDLE_MANIFEST, recursive=False)
            nonce = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(nonce, tar_path.read_bytes(), MAGIC)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_destination = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            try:
                temporary_destination.write_bytes(MAGIC + nonce + ciphertext)
                os.replace(temporary_destination, destination)
            finally:
                temporary_destination.unlink(missing_ok=True)
        return manifest

    @staticmethod
    def restore(bundle: Path, destination: Path, *, key: bytes) -> BackupManifest:
        raw = bundle.read_bytes()
        if not raw.startswith(MAGIC) or len(key) != 32:
            raise ValueError("invalid backup bundle")
        nonce, ciphertext = raw[len(MAGIC):len(MAGIC) + 12], raw[len(MAGIC) + 12:]
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, MAGIC)
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("restore destination must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="finscope-restore-") as temporary:
            tar_path = Path(temporary) / "payload.tar"
            tar_path.write_bytes(plaintext)
            with tarfile.open(tar_path, "r") as archive:
                for member in archive.getmembers():
                    target = (destination / member.name).resolve()
                    if destination.resolve() not in target.parents and target != destination.resolve():
                        raise ValueError("unsafe backup member path")
                archive.extractall(destination, filter="data")
        manifest_data = json.loads((destination / BUNDLE_MANIFEST).read_text(encoding="utf-8"))
        manifest = BackupManifest(**manifest_data)
        for relative, expected in manifest.files.items():
            path = destination / relative
            if not path.is_file() or _sha256(path) != expected["sha256"] or path.stat().st_size != expected["size"]:
                raise ValueError("backup file integrity check failed")
        return manifest
