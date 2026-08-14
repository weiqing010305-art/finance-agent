from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql


ROLE_OPTIONS = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"


def _secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE")
    if not path:
        raise RuntimeError(f"{name}_FILE is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{name} is empty")
    return value


def provision() -> None:
    database_url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    credentials = {
        "finscope_app": _secret("POSTGRES_APP_PASSWORD"),
        "finscope_worker": _secret("POSTGRES_WORKER_PASSWORD"),
    }
    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for role, password in credentials.items():
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if cursor.fetchone() is None:
                    cursor.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} " + ROLE_OPTIONS).format(
                            sql.Identifier(role), sql.Literal(password)
                        )
                    )
                else:
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {} " + ROLE_OPTIONS).format(
                            sql.Identifier(role), sql.Literal(password)
                        )
                    )
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = 'finscope_security_owner'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "CREATE ROLE finscope_security_owner NOLOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT BYPASSRLS"
                )


if __name__ == "__main__":
    provision()
