from pathlib import Path

from backend.db.metadata import invitations, memberships, refresh_tokens, tenant_resources


def test_all_private_foundation_tables_have_tenant_key():
    for table in (memberships, invitations, refresh_tokens, tenant_resources):
        assert "tenant_id" in table.c


def test_initial_migration_enables_and_forces_rls_with_missing_context_safe_expression():
    migration = Path("alembic/versions/0001_identity_and_rls.py").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "current_setting('app.tenant_id', true)" in migration
    assert "WITH CHECK" in migration


def test_worker_handoff_function_is_narrow_fixed_path_and_not_public():
    migration = Path("alembic/versions/0007_worker_capability_handoff.py").read_text(encoding="utf-8")
    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog, public" in migration
    assert "RETURNS TABLE(tenant_id text, user_id text, role text)" in migration
    assert "REVOKE ALL ON FUNCTION" in migration
    assert "GRANT EXECUTE" in migration and "finscope_worker" in migration
    assert "payload_json" not in migration


def test_due_delivery_function_exposes_identity_only_and_throttles_stale_claims():
    migration = Path("alembic/versions/0008_due_job_dispatch.py").read_text(encoding="utf-8")
    assert "RETURNS TABLE(job_id text, tenant_id text, user_id text, role text)" in migration
    assert "SECURITY DEFINER" in migration and "SET search_path = pg_catalog, public" in migration
    assert "claim_expires_at < clock_timestamp()" in migration
    assert "interval '30 seconds'" in migration
    assert "payload_json" not in migration


def test_latest_migrations_add_tenant_fks_least_privilege_and_exhausted_recovery():
    integrity = Path("alembic/versions/0009_tenant_integrity_and_least_privilege.py").read_text(encoding="utf-8")
    recovery = Path("alembic/versions/0010_job_recovery_guards.py").read_text(encoding="utf-8")
    assert "fk_job_outbox_job_tenant" in integrity
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES" in integrity
    assert "ALTER DEFAULT PRIVILEGES" in integrity
    assert "finscope_security_owner" in integrity
    assert "finscope_reconcile_exhausted_jobs" in recovery
    assert "status = 'dead'" in recovery and "status = 'failed'" in recovery


def test_auth_hardening_removes_unused_identity_writes_and_tenant_fences_users():
    hardening = Path("alembic/versions/0011_auth_role_hardening.py").read_text(encoding="utf-8")
    assert "REVOKE UPDATE ON users" in hardening
    assert "REVOKE ALL PRIVILEGES ON tenants" in hardening
    assert "REVOKE UPDATE, DELETE ON memberships" in hardening
    assert "FORCE ROW LEVEL SECURITY" in hardening
    assert "users_tenant_or_invitation" in hardening


def test_migration_downgrades_restore_previous_delivery_function_and_runtime_acls():
    integrity = Path("alembic/versions/0009_tenant_integrity_and_least_privilege.py").read_text(encoding="utf-8")
    recovery = Path("alembic/versions/0010_job_recovery_guards.py").read_text(encoding="utf-8")
    assert "o.published_at IS NULL" in recovery.split("def downgrade", 1)[1]
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT" in integrity.split("def downgrade", 1)[1]
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" in integrity.split("def downgrade", 1)[1]
