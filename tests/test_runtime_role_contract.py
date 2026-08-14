from pathlib import Path


def test_compose_separates_migration_api_and_worker_roles_without_password_in_url():
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    assert "DATABASE_ROLE: finscope_admin" in compose
    assert "DATABASE_ROLE: finscope_app" in compose
    assert "DATABASE_ROLE: finscope_worker" in compose
    assert "postgresql+psycopg://" not in compose
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_admin_password" in compose
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_app_password" in compose
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_worker_password" in compose
    assert "/run/secrets/postgres_password\n" not in compose
    assert "  postgres_password:" not in compose


def test_runtime_roles_are_non_superuser_and_rls_cannot_be_bypassed():
    script = Path("infra/postgres/init-roles.sh").read_text(encoding="utf-8")
    assert script.count("NOSUPERUSER") == 2
    assert script.count("NOBYPASSRLS") == 2
    assert "PASSWORD %L" in script
    assert ":'worker_password'" in script
    migration = Path("alembic/versions/0006_runtime_roles.py").read_text(encoding="utf-8")
    assert "REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM finscope_app" in migration


def test_entrypoint_uses_pgpass_not_password_environment_or_database_url():
    entrypoint = Path("scripts/container_entrypoint.py").read_text(encoding="utf-8")
    assert "PGPASSFILE" in entrypoint
    assert "DATABASE_URL" in entrypoint
    assert "postgresql+psycopg://{role}@" in entrypoint
    assert "PGPASSWORD" not in entrypoint


def test_runtime_images_contain_all_entrypoints_and_migrations():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    for required in ("COPY scripts ./scripts", "COPY alembic ./alembic", "COPY alembic.ini ./alembic.ini"):
        assert required in dockerfile


def test_api_and_worker_never_mount_admin_database_secret():
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    api = compose.split("  api:", 1)[1].split("  worker:", 1)[0]
    worker = compose.split("  worker:", 1)[1].split("  dispatcher:", 1)[0]
    dispatcher = compose.split("  dispatcher:", 1)[1].split("  migrate:", 1)[0]
    assert "postgres_admin_password" not in api
    assert "postgres_admin_password" not in worker
    assert "postgres_admin_password" not in dispatcher


def test_live_role_verifier_checks_admin_assumption_and_schema_create():
    verifier = Path("scripts/verify_runtime_role.py").read_text(encoding="utf-8")
    assert "SET ROLE finscope_admin" in verifier
    assert "InsufficientPrivilege" in verifier
    assert "has_schema_privilege" in verifier and "CREATE" in verifier
