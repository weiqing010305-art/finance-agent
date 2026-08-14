"""Port the durable run/plan/checkpoint/lease/step/event core to PostgreSQL."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_durable_runner_core"
down_revision = "0003_authorized_retrieval"
branch_labels = None
depends_on = None

TABLES = (
    "research_runs_pg", "research_plans_pg", "research_checkpoints_pg",
    "research_leases_pg", "research_steps_pg", "research_events_pg",
)


def upgrade() -> None:
    op.create_table(
        "research_runs_pg", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(64), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("company", sa.String(200), nullable=False), sa.Column("question", sa.Text(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("budget_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('running','pause_requested','paused','resuming','failed','completed')", name="ck_research_run_pg_status"),
        sa.CheckConstraint("progress BETWEEN 0 AND 100 AND budget_used >= 0", name="ck_research_run_pg_progress"),
        sa.UniqueConstraint("tenant_id", "created_by", "idempotency_key", name="uq_research_run_pg_idempotency"),
    )
    op.create_index("ix_research_runs_pg_tenant_status", "research_runs_pg", ["tenant_id", "status"])
    op.create_table("research_plans_pg",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True), sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("research_checkpoints_pg",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("next_pointer", sa.String(128), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("research_leases_pg",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("research_steps_pg",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("step_id", sa.String(128), primary_key=True), sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False), sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("research_events_pg",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False), sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_research_events_pg_run", "research_events_pg", ["tenant_id", "run_id"])
    for table in TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        op.execute(sa.text(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))"))


def downgrade() -> None:
    for table in reversed(TABLES): op.drop_table(table)
