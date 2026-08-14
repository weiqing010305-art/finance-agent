"""Add the narrowly scoped worker job-context resolver."""
from __future__ import annotations

from alembic import op

revision = "0007_worker_handoff"
down_revision = "0006_runtime_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.finscope_job_context(p_job_id text)
        RETURNS TABLE(tenant_id text, user_id text, role text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT j.tenant_id::text, j.created_by::text, m.role::text
            FROM public.jobs AS j
            JOIN public.memberships AS m
              ON m.tenant_id = j.tenant_id AND m.user_id = j.created_by
            WHERE j.id = p_job_id
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.finscope_job_context(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.finscope_job_context(text) TO finscope_worker")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS public.finscope_job_context(text)")
