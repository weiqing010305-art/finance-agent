from __future__ import annotations

import argparse
import os
from pathlib import Path

from backend.backup import EncryptedBackupBundle, read_backup_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt a staged PostgreSQL/MinIO FinScope backup")
    parser.add_argument("--staging-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--type", choices=["full", "incremental"], required=True)
    parser.add_argument("--schema-revision", required=True)
    parser.add_argument("--parent")
    args = parser.parse_args()
    key_file = os.environ.get("BACKUP_ENCRYPTION_KEY_FILE")
    if not key_file:
        raise SystemExit("BACKUP_ENCRYPTION_KEY_FILE is required")
    EncryptedBackupBundle.create(
        Path(args.staging_dir), Path(args.output), key=read_backup_key(key_file),
        backup_type=args.type, schema_revision=args.schema_revision, parent_backup=args.parent,
    )


if __name__ == "__main__": main()
