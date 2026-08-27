import json
import os
import subprocess
import sys

import pytest
from cryptography.exceptions import InvalidTag

from backend.backup import EncryptedBackupBundle, read_backup_key


def test_encrypted_bundle_roundtrip_hashes_and_contains_no_plaintext_secret(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    (source / "postgres.dump").write_bytes(b"DATABASE-CONTENT")
    (source / "minio").mkdir(); (source / "minio" / "object.bin").write_bytes(b"PRIVATE-OBJECT")
    bundle, key = tmp_path / "backup.fsbk", os.urandom(32)
    manifest = EncryptedBackupBundle.create(
        source, bundle, key=key, backup_type="full", schema_revision="0005_evidence_memory_audit",
    )
    raw = bundle.read_bytes()
    assert b"DATABASE-CONTENT" not in raw and b"PRIVATE-OBJECT" not in raw
    restored = tmp_path / "isolated-restore"
    recovered = EncryptedBackupBundle.restore(bundle, restored, key=key)
    assert recovered.files == manifest.files
    assert (restored / "postgres.dump").read_bytes() == b"DATABASE-CONTENT"


def test_tamper_or_wrong_key_fails_authenticated_decryption(tmp_path):
    source = tmp_path / "source"; source.mkdir(); (source / "x").write_text("secret")
    bundle, key = tmp_path / "backup.fsbk", os.urandom(32)
    EncryptedBackupBundle.create(source, bundle, key=key, backup_type="incremental", schema_revision="v")
    raw = bytearray(bundle.read_bytes()); raw[-1] ^= 1; bundle.write_bytes(raw)
    with pytest.raises(InvalidTag):
        EncryptedBackupBundle.restore(bundle, tmp_path / "restore", key=key)


def test_backup_never_overwrites_or_deletes_a_source_manifest(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    original = source / "manifest.json"
    original.write_text("PRIVATE ORIGINAL", encoding="utf-8")
    bundle = tmp_path / "bundle.enc"
    EncryptedBackupBundle.create(
        source, bundle, key=b"k" * 32, backup_type="full", schema_revision="0010",
    )
    assert original.read_text(encoding="utf-8") == "PRIVATE ORIGINAL"
    restored = tmp_path / "restored"
    manifest = EncryptedBackupBundle.restore(bundle, restored, key=b"k" * 32)
    assert manifest.files["manifest.json"]["size"] == len("PRIVATE ORIGINAL")
    assert (restored / "manifest.json").read_text(encoding="utf-8") == "PRIVATE ORIGINAL"


def test_backup_key_accepts_unpadded_urlsafe_base64(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("a" * 43, encoding="ascii")
    assert len(read_backup_key(key_file)) == 32


def test_formal_backup_collects_database_and_minio_then_drills_isolated_restore():
    script = open("scripts/backup_formal.ps1", encoding="utf-8").read()
    minio_script = open("scripts/minio_backup.sh", encoding="utf-8").read()
    postgres_script = open("scripts/postgres_backup_snapshot.sh", encoding="utf-8").read()
    assert "pg_dump" in postgres_script and "pg_restore" in script
    assert "mc mirror" in minio_script and "finscope-restore-" in script
    assert "Assert-SafeTemporaryPath" in script
    assert "--exit-on-error" in script and "alembic_version" in script
    schedule = open("scripts/install_backup_schedule.ps1", encoding="utf-8").read()
    assert "RepetitionInterval" in schedule and "New-TimeSpan -Hours 1" in schedule
    assert "StartWhenAvailable" in schedule and "MultipleInstances IgnoreNew" in schedule


def test_bundle_cli_requires_exact_database_object_inventory(tmp_path):
    source = tmp_path / "source"; (source / "minio/private/t/o/hash").mkdir(parents=True)
    body = b"PRIVATE-OBJECT"
    object_path = source / "minio/private/t/o/hash/value"
    object_path.write_bytes(body)
    (source / "postgres.dump").write_bytes(b"PG")
    digest = __import__("hashlib").sha256(body).hexdigest()
    inventory = source / "object-inventory.tsv"
    inventory.write_text(f"private/t/o/hash/value\t{len(body)}\t{digest}\n", encoding="utf-8")
    schema = source / "schema-revision.txt"
    schema.write_text("0016_grant_run_delete\n", encoding="utf-8")
    key = tmp_path / "key"; key.write_bytes(b"k" * 32)
    bundle = tmp_path / "backup.fsbk"
    command = [sys.executable, "-m", "scripts.backup_bundle_cli", "create", "--source", str(source),
               "--bundle", str(bundle), "--key-file", str(key), "--schema-revision-file", str(schema),
               "--object-inventory", str(inventory)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    object_path.write_bytes(b"WRONG")
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0 and "identity mismatch" in (failed.stdout + failed.stderr)
    object_path.write_bytes(body)
    schema.write_text("wrong_revision\n", encoding="utf-8")
    wrong_schema = subprocess.run(command, capture_output=True, text=True)
    assert wrong_schema.returncode != 0 and "schema revision" in (wrong_schema.stdout + wrong_schema.stderr)


def test_postgres_and_minio_backup_scripts_use_one_snapshot_and_exact_keys():
    postgres = open("scripts/postgres_backup_snapshot.sh", encoding="utf-8").read()
    minio = open("scripts/minio_backup.sh", encoding="utf-8").read()
    formal = open("scripts/backup_formal.ps1", encoding="utf-8").read()
    assert "pg_export_snapshot" in postgres and '--snapshot="$snapshot_id"' in postgres
    assert "SET TRANSACTION SNAPSHOT" in postgres
    assert "SELECT version_num FROM alembic_version" in postgres
    assert "WHERE status = 'ready'" in postgres and "verified_size, sha256" in postgres
    assert "mc mirror --overwrite local/finscope-private" not in minio
    assert 'mkdir -p "$path"' in minio
    assert "mc cp --quiet" in minio and "object-inventory.tsv" in formal


def test_restore_cli_rejects_bundle_from_unsupported_schema(tmp_path):
    source = tmp_path / "source"; source.mkdir(); (source / "postgres.dump").write_bytes(b"PG")
    key_path = tmp_path / "key"; key_path.write_bytes(b"k" * 32)
    bundle = tmp_path / "legacy.fsbk"
    EncryptedBackupBundle.create(
        source, bundle, key=b"k" * 32, backup_type="full", schema_revision="legacy_revision",
    )
    command = [sys.executable, "-m", "scripts.backup_bundle_cli", "restore", "--bundle", str(bundle),
               "--destination", str(tmp_path / "restore"), "--key-file", str(key_path)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0 and "schema revision" in (result.stdout + result.stderr)
