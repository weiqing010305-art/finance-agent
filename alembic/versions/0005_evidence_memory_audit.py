"""Add tenant-safe evidence, report, memory and 90-day audit aggregates."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_evidence_memory_audit"
down_revision = "0004_durable_runner_core"
branch_labels = None
depends_on = None
TABLES = ("evidence_items_pg", "claims_pg", "reports_pg", "memory_records_pg", "audit_events_pg")


def upgrade() -> None:
    op.create_table("evidence_items_pg",
        sa.Column("id", sa.String(128), primary_key=True), sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False), sa.Column("source_uri", sa.Text(), nullable=False), sa.Column("authority_tier", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("authority_tier BETWEEN 0 AND 5", name="ck_evidence_pg_authority"))
    op.create_index("ix_evidence_items_pg_run", "evidence_items_pg", ["tenant_id", "run_id"])
    op.create_table("claims_pg",
        sa.Column("id", sa.String(128), primary_key=True), sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False), sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("confidence", sa.Integer(), nullable=False), sa.Column("evidence_ids_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("status IN ('supported','insufficient','contradicted')", name="ck_claim_pg_status"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_claim_pg_confidence"))
    op.create_index("ix_claims_pg_run", "claims_pg", ["tenant_id", "run_id"])
    op.create_table("reports_pg",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False), sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False), sa.Column("citations_json", sa.Text(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("memory_records_pg",
        sa.Column("id", sa.String(64), primary_key=True), sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("memory_key", sa.String(128), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("source_run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="SET NULL")),
        sa.Column("source_claim_id", sa.String(128), sa.ForeignKey("claims_pg.id", ondelete="SET NULL")), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("memory_type IN ('user_preference','company_fact','entity_identity')", name="ck_memory_pg_type"),
        sa.CheckConstraint("status IN ('active','expired','tombstoned')", name="ck_memory_pg_status"),
        sa.UniqueConstraint("tenant_id", "user_id", "memory_type", "memory_key", name="uq_memory_pg_key"))
    op.create_index("ix_memory_records_pg_expiry", "memory_records_pg", ["tenant_id", "status", "expires_at"])
    op.create_table("audit_events_pg",
        sa.Column("id", sa.String(64), primary_key=True), sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False), sa.Column("target_id", sa.String(128), nullable=False), sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_audit_events_pg_expiry", "audit_events_pg", ["expires_at"])
    for table in TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" USING (tenant_id = NULLIF(current_setting(\'app.tenant_id\', true), \'\')) WITH CHECK (tenant_id = NULLIF(current_setting(\'app.tenant_id\', true), \'\'))'))


def downgrade() -> None:
    for table in reversed(TABLES): op.drop_table(table)
