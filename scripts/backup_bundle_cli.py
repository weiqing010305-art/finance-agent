from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from backend.backup import EncryptedBackupBundle, read_backup_key
from backend.schema_compat import CURRENT_ALEMBIC_REVISION


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or verify a FinScope encrypted backup bundle")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create")
    create.add_argument("--source", required=True)
    create.add_argument("--bundle", required=True)
    create.add_argument("--key-file", required=True)
    create.add_argument("--schema-revision-file", required=True)
    create.add_argument("--object-inventory", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--bundle", required=True)
    restore.add_argument("--destination", required=True)
    restore.add_argument("--key-file", required=True)
    args = parser.parse_args()
    if args.action == "create":
        source, inventory_path = Path(args.source), Path(args.object_inventory)
        schema_revision = Path(args.schema_revision_file).read_text(encoding="utf-8").strip()
        if schema_revision != CURRENT_ALEMBIC_REVISION:
            raise SystemExit("database schema revision is not supported by this runtime")
        expected: dict[str, tuple[int, str]] = {}
        for line in inventory_path.read_text(encoding="utf-8").splitlines():
            key, size, digest = line.split("\t")
            candidate = Path(key)
            if candidate.is_absolute() or ".." in candidate.parts or key in expected:
                raise SystemExit("unsafe or duplicate object inventory key")
            expected[key] = (int(size), digest)
        collected = {
            path.relative_to(source / "minio").as_posix(): path
            for path in (source / "minio").rglob("*") if path.is_file()
        }
        if set(collected) != set(expected):
            raise SystemExit("MinIO backup key inventory mismatch")
        for key, (size, digest) in expected.items():
            path = collected[key]
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.stat().st_size != size or actual != digest:
                raise SystemExit(f"MinIO backup object identity mismatch: {key}")
        manifest = EncryptedBackupBundle.create(
            source, Path(args.bundle), key=read_backup_key(args.key_file),
            backup_type="full", schema_revision=schema_revision,
        )
    else:
        manifest = EncryptedBackupBundle.restore(
            Path(args.bundle), Path(args.destination), key=read_backup_key(args.key_file),
        )
        if manifest.schema_revision != CURRENT_ALEMBIC_REVISION:
            raise SystemExit("backup schema revision is not supported by this runtime")
    print(json.dumps(manifest.__dict__, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
