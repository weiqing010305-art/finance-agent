from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("container entrypoint requires a command")
    secret_file = os.environ.get("POSTGRES_PASSWORD_FILE", "/run/secrets/postgres_password")
    password = Path(secret_file).read_text(encoding="utf-8").strip()
    if not password or any(character in password for character in "\r\n:"):
        raise SystemExit("invalid PostgreSQL password secret")
    role = os.environ.get("DATABASE_ROLE", "finscope_app")
    if role not in {"finscope_admin", "finscope_app", "finscope_worker"}:
        raise SystemExit("invalid database role")
    pgpass = Path("/tmp/finscope.pgpass")
    pgpass.write_text(f"postgres:5432:finscope:{role}:{password}\n", encoding="utf-8")
    pgpass.chmod(0o600)
    os.environ["PGPASSFILE"] = str(pgpass)
    os.environ["DATABASE_URL"] = f"postgresql+psycopg://{role}@postgres:5432/finscope"
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__": main()
