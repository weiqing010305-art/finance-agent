from __future__ import annotations

import argparse
import os
from pathlib import Path

from backend.backup import EncryptedBackupBundle, read_backup_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Decrypt a FinScope backup into an isolated staging directory")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    destination = Path(args.destination).resolve()
    if "restore" not in destination.name.casefold():
        raise SystemExit("destination name must contain 'restore' to prevent accidental overwrite")
    key_file = os.environ.get("BACKUP_ENCRYPTION_KEY_FILE")
    if not key_file:
        raise SystemExit("BACKUP_ENCRYPTION_KEY_FILE is required")
    EncryptedBackupBundle.restore(Path(args.bundle), destination, key=read_backup_key(key_file))


if __name__ == "__main__": main()
