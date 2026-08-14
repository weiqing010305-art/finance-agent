"""Add durable job ledger, outbox and private object metadata."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_jobs_and_objects"
down_revision = "0001_identity_and_rls"
branch_labels = None
depends_on = None

_TABLES = ("jobs", "job_outbox", "objects")


def _rls(table: str) -> None:
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(
        f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))"
    ))


def upgrade() -> None:
    op.create_table(
        "jobs", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(64), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False), sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("claim_token_hash", sa.String(64)), sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','running','retry','completed','dead')", name="ck_job_status"),
        sa.CheckConstraint("attempt >= 0 AND max_attempts > 0", name="ck_job_attempts"),
    )
    op.create_index("ix_jobs_due", "jobs", ["status", "next_attempt_at"])
    op.create_index("ix_jobs_tenant", "jobs", ["tenant_id"])
    op.create_table(
        "job_outbox", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_outbox_unpublished", "job_outbox", ["published_at"])
    op.create_table(
        "objects", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("quarantine_key", sa.String(512), nullable=False, unique=True),
        sa.Column("object_key", sa.String(512), unique=True),
        sa.Column("declared_mime", sa.String(128), nullable=False), sa.Column("verified_mime", sa.String(128)),
        sa.Column("declared_size", sa.BigInteger(), nullable=False), sa.Column("verified_size", sa.BigInteger()),
        sa.Column("sha256", sa.String(64)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending','quarantined','ready','rejected','tombstoned','deleted')", name="ck_object_status"),
        sa.CheckConstraint("declared_size > 0", name="ck_object_declared_size"),
    )
    op.create_index("ix_objects_tenant_status", "objects", ["tenant_id", "status"])
    for table in _TABLES:
        _rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
