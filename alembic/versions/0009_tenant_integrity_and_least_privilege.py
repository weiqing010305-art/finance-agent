"""Enforce tenant-parent identity and replace broad runtime grants."""
from __future__ import annotations

from alembic import op

revision = "0009_tenant_integrity"
down_revision = "0008_due_job_dispatch"
branch_labels = None
depends_on = None


RUN_CHILDREN = {
    "research_plans_pg": "fk_research_plans_run_tenant",
    "research_checkpoints_pg": "fk_research_checkpoints_run_tenant",
    "research_leases_pg": "fk_research_leases_run_tenant",
    "research_steps_pg": "fk_research_steps_run_tenant",
    "research_events_pg": "fk_research_events_run_tenant",
    "evidence_items_pg": "fk_evidence_items_run_tenant",
    "claims_pg": "fk_claims_run_tenant",
    "reports_pg": "fk_reports_run_tenant",
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.create_unique_constraint("uq_jobs_id_tenant", "jobs", ["id", "tenant_id"])
    op.create_unique_constraint("uq_research_runs_pg_id_tenant", "research_runs_pg", ["id", "tenant_id"])
    op.create_unique_constraint("uq_claims_pg_id_tenant", "claims_pg", ["id", "tenant_id"])
    op.create_foreign_key(
        "fk_job_outbox_job_tenant", "job_outbox", "jobs",
        ["job_id", "tenant_id"], ["id", "tenant_id"], ondelete="CASCADE",
    )
    for table, constraint_name in RUN_CHILDREN.items():
        op.create_foreign_key(
            constraint_name, table, "research_runs_pg",
            ["run_id", "tenant_id"], ["id", "tenant_id"], ondelete="CASCADE",
        )
    op.create_foreign_key(
        "fk_memory_source_run_tenant", "memory_records_pg", "research_runs_pg",
        ["source_run_id", "tenant_id"], ["id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_memory_source_claim_tenant", "memory_records_pg", "claims_pg",
        ["source_claim_id", "tenant_id"], ["id", "tenant_id"],
    )

    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM finscope_app, finscope_worker")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM finscope_app, finscope_worker")
    op.execute("GRANT SELECT ON alembic_version TO finscope_app, finscope_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE ON users TO finscope_app")
    op.execute("GRANT SELECT, INSERT ON tenants TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON invitations, refresh_tokens TO finscope_app")
    op.execute("GRANT SELECT, INSERT ON tenant_resources TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON jobs, job_outbox, objects TO finscope_app")
    op.execute("GRANT SELECT ON retrieval_chunks TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON research_runs_pg TO finscope_app")
    op.execute("GRANT SELECT, INSERT ON research_plans_pg TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON research_checkpoints_pg TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON research_leases_pg TO finscope_app")
    op.execute("GRANT SELECT ON research_steps_pg, evidence_items_pg, claims_pg, reports_pg, memory_records_pg, audit_events_pg TO finscope_app")
    op.execute("GRANT SELECT, INSERT ON research_events_pg TO finscope_app")

    op.execute("GRANT SELECT, INSERT, UPDATE ON jobs, job_outbox, objects TO finscope_worker")
    op.execute("GRANT SELECT ON memberships, retrieval_chunks TO finscope_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE ON research_runs_pg, research_checkpoints_pg TO finscope_worker")
    op.execute("GRANT SELECT, INSERT ON research_plans_pg, research_steps_pg, research_events_pg, evidence_items_pg, claims_pg, reports_pg, audit_events_pg TO finscope_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON research_leases_pg TO finscope_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE ON memory_records_pg TO finscope_worker")

    op.execute("GRANT USAGE ON SCHEMA public TO finscope_security_owner")
    op.execute("GRANT SELECT ON jobs, job_outbox, memberships TO finscope_security_owner")
    op.execute("ALTER FUNCTION public.finscope_job_context(text) OWNER TO finscope_security_owner")
    op.execute("ALTER FUNCTION public.finscope_due_job_deliveries(integer) OWNER TO finscope_security_owner")
    op.execute("REVOKE ALL ON FUNCTION public.finscope_job_context(text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.finscope_due_job_deliveries(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.finscope_job_context(text) TO finscope_worker")
    op.execute("GRANT EXECUTE ON FUNCTION public.finscope_due_job_deliveries(integer) TO finscope_worker")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER FUNCTION public.finscope_job_context(text) OWNER TO finscope_admin")
    op.execute("ALTER FUNCTION public.finscope_due_job_deliveries(integer) OWNER TO finscope_admin")
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM finscope_app, finscope_worker")
    op.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM finscope_app, finscope_worker")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM finscope_app, finscope_worker")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM finscope_app, finscope_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO finscope_app")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM finscope_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON jobs, job_outbox, objects, research_runs_pg, research_plans_pg, research_checkpoints_pg, research_leases_pg, research_steps_pg, research_events_pg, evidence_items_pg, claims_pg, reports_pg, memory_records_pg, audit_events_pg, retrieval_chunks TO finscope_worker")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finscope_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO finscope_app")
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM finscope_security_owner")
    op.execute("REVOKE USAGE ON SCHEMA public FROM finscope_security_owner")
    op.drop_constraint("fk_memory_source_claim_tenant", "memory_records_pg", type_="foreignkey")
    op.drop_constraint("fk_memory_source_run_tenant", "memory_records_pg", type_="foreignkey")
    for table, constraint_name in reversed(tuple(RUN_CHILDREN.items())):
        op.drop_constraint(constraint_name, table, type_="foreignkey")
    op.drop_constraint("fk_job_outbox_job_tenant", "job_outbox", type_="foreignkey")
    op.drop_constraint("uq_claims_pg_id_tenant", "claims_pg", type_="unique")
    op.drop_constraint("uq_research_runs_pg_id_tenant", "research_runs_pg", type_="unique")
    op.drop_constraint("uq_jobs_id_tenant", "jobs", type_="unique")
