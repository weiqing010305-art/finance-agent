from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine

from backend.auth.store import AuthStore
from backend.auth.tokens import TokenCodec


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first FinScope owner and tenant")
    parser.add_argument("--email", required=True)
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Owner password: ")
    if not password:
        raise SystemExit("owner password must not be empty")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    signing_key = os.environ.get("JWT_SIGNING_KEY", "").strip()
    signing_file = os.environ.get("JWT_SIGNING_KEY_FILE", "").strip()
    if signing_key and signing_file:
        raise SystemExit("configure only one JWT signing-key source")
    if signing_file:
        signing_key = Path(signing_file).read_text(encoding="utf-8").strip()
    if not database_url or not signing_key:
        raise SystemExit("DATABASE_URL and JWT signing key are required")
    store = AuthStore(create_engine(database_url), TokenCodec(signing_key))
    principal = store.bootstrap_owner(email=args.email, password=password, tenant_name=args.tenant_name)
    print(f"bootstrap complete: tenant={principal.tenant_id} user={principal.user_id}")


if __name__ == "__main__":
    main()
