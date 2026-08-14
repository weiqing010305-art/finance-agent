"""Add narrow global discovery for due broker deliveries."""
from __future__ import annotations

from alembic import op

revision = "0008_due_job_dispatch"
down_revision = "0007_worker_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.finscope_due_job_deliveries(p_limit integer)
        RETURNS TABLE(job_id text, tenant_id text, user_id text, role text)
        LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT j.id::text, j.tenant_id::text, j.created_by::text, m.role::text
            FROM public.jobs AS j
            JOIN public.job_outbox AS o ON o.job_id = j.id
            JOIN public.memberships AS m
              ON m.tenant_id = j.tenant_id AND m.user_id = j.created_by
            WHERE j.attempt < j.max_attempts
              AND (
                (j.status IN ('pending', 'retry') AND j.next_attempt_at <= clock_timestamp()
                 AND o.published_at IS NULL)
                OR
                (j.status = 'running' AND j.claim_expires_at < clock_timestamp()
                 AND (o.published_at IS NULL
                      OR o.published_at < clock_timestamp() - interval '30 seconds'))
              )
            ORDER BY j.next_attempt_at, j.created_at
            LIMIT LEAST(GREATEST(COALESCE(p_limit, 1), 1), 1000)
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.finscope_due_job_deliveries(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.finscope_due_job_deliveries(integer) TO finscope_worker")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS public.finscope_due_job_deliveries(integer)")
