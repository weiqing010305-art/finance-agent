"""Fence user rows by tenant and remove unused app identity writes."""
from __future__ import annotations

from alembic import op

revision = "0011_auth_role_hardening"
down_revision = "0010_job_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("REVOKE UPDATE ON users FROM finscope_app")
    op.execute("REVOKE ALL PRIVILEGES ON tenants FROM finscope_app")
    op.execute("REVOKE UPDATE, DELETE ON memberships FROM finscope_app")
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY users_tenant_or_invitation ON users
        USING (
          EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.user_id = users.id
              AND m.tenant_id = current_setting('app.tenant_id', true)
          )
          OR EXISTS (
            SELECT 1 FROM invitations AS i
            WHERE i.email = users.email
              AND i.tenant_id = current_setting('app.tenant_id', true)
              AND i.accepted_at IS NULL AND i.revoked_at IS NULL
              AND i.expires_at > clock_timestamp()
          )
        )
        WITH CHECK (
          EXISTS (
            SELECT 1 FROM invitations AS i
            WHERE i.email = users.email
              AND i.tenant_id = current_setting('app.tenant_id', true)
              AND i.accepted_at IS NULL AND i.revoked_at IS NULL
              AND i.expires_at > clock_timestamp()
          )
        )
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP POLICY IF EXISTS users_tenant_or_invitation ON users")
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")
    op.execute("GRANT UPDATE ON users TO finscope_app")
    op.execute("GRANT SELECT, INSERT ON tenants TO finscope_app")
    op.execute("GRANT UPDATE, DELETE ON memberships TO finscope_app")
