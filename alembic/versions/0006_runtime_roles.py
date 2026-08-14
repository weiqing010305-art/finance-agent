"""Grant least-privilege runtime roles after all Phase 6 tables exist."""
from __future__ import annotations

from alembic import op

revision = "0006_runtime_roles"
down_revision = "0005_evidence_memory_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    op.execute("GRANT CONNECT ON DATABASE finscope TO finscope_app, finscope_worker")
    op.execute("GRANT USAGE ON SCHEMA public TO finscope_app, finscope_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO finscope_app")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM finscope_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON jobs, job_outbox, objects, research_runs_pg, research_plans_pg, research_checkpoints_pg, research_leases_pg, research_steps_pg, research_events_pg, evidence_items_pg, claims_pg, reports_pg, memory_records_pg, audit_events_pg, retrieval_chunks TO finscope_worker")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finscope_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO finscope_app")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM finscope_app, finscope_worker")
    op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM finscope_app, finscope_worker")
