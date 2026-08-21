"""Create execution_authorizations_pg for the controlled-tools policy audit trail."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_execution_authorizations"
down_revision = "0013_evidence_bibliography"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_authorizations_pg",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("plan_version", sa.Integer(), primary_key=True),
        sa.Column("step_id", sa.String(128), primary_key=True),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_codes_json", sa.Text(), nullable=False),
        sa.Column("estimated_cost", sa.Integer(), nullable=False),
        sa.Column("budget_before", sa.Integer(), nullable=False),
        sa.Column("effective_cost", sa.Integer(), nullable=False),
        sa.Column("capability_token", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["research_runs_pg.id", "research_runs_pg.tenant_id"],
            ondelete="CASCADE",
            name="fk_exec_auth_run_tenant",
        ),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON execution_authorizations_pg TO finscope_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON execution_authorizations_pg TO finscope_app")


def downgrade() -> None:
    op.execute("REVOKE ALL PRIVILEGES ON execution_authorizations_pg FROM finscope_worker")
    op.execute("REVOKE ALL PRIVILEGES ON execution_authorizations_pg FROM finscope_app")
    op.drop_table("execution_authorizations_pg")