from __future__ import annotations

import os

import psycopg
from psycopg import errors


def main() -> None:
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            role, superuser, bypass_rls = cursor.fetchone()
            if role not in {"finscope_app", "finscope_worker"} or superuser or bypass_rls:
                raise SystemExit("runtime role is over-privileged")
            try:
                cursor.execute("SET ROLE finscope_admin")
            except errors.InsufficientPrivilege:
                connection.rollback()
            else:
                raise SystemExit("runtime role can assume the admin role")
            cursor.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            if cursor.fetchone()[0]:
                raise SystemExit("runtime role can create public schema objects")
    print(f"runtime_role_verified role={role}")


if __name__ == "__main__":
    main()
