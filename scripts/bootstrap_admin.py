from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import create_engine

from backend.auth.store import AuthStore
from backend.auth.tokens import TokenCodec
from backend.settings import RuntimeSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first FinScope owner and tenant")
    parser.add_argument("--email", required=True)
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Owner password: ")
    if not password:
        raise SystemExit("owner password must not be empty")
    settings = RuntimeSettings.from_env()
    store = AuthStore(create_engine(settings.database_url), TokenCodec(settings.jwt_signing_key))
    principal = store.bootstrap_owner(email=args.email, password=password, tenant_name=args.tenant_name)
    print(f"bootstrap complete: tenant={principal.tenant_id} user={principal.user_id}")


if __name__ == "__main__":
    main()
