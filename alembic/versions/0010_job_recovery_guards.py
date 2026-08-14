"""Close broker-loss and exhausted-claim recovery windows."""
from __future__ import annotations

from alembic import op

revision = "0010_job_recovery"
down_revision = "0009_tenant_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
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
            JOIN public.job_outbox AS o ON o.job_id = j.id AND o.tenant_id = j.tenant_id
            JOIN public.memberships AS m
              ON m.tenant_id = j.tenant_id AND m.user_id = j.created_by
            WHERE j.attempt < j.max_attempts
              AND (
                (j.status IN ('pending', 'retry') AND j.next_attempt_at <= clock_timestamp()
                 AND (o.published_at IS NULL
                      OR o.published_at < clock_timestamp() - interval '30 seconds'))
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
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.finscope_reconcile_exhausted_jobs(p_limit integer)
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            candidate record;
            failed_version integer;
            reconciled integer := 0;
        BEGIN
            FOR candidate IN
                SELECT j.id, j.tenant_id, j.payload_json
                FROM public.jobs AS j
                WHERE j.status IN ('pending', 'retry', 'running')
                  AND (
                    (j.status = 'running' AND j.claim_expires_at < clock_timestamp()
                     AND j.attempt >= j.max_attempts)
                    OR NOT EXISTS (
                      SELECT 1 FROM public.memberships AS m
                      WHERE m.tenant_id = j.tenant_id AND m.user_id = j.created_by
                    )
                  )
                ORDER BY j.updated_at
                LIMIT LEAST(GREATEST(COALESCE(p_limit, 1), 1), 1000)
                FOR UPDATE SKIP LOCKED
            LOOP
                UPDATE public.jobs SET status = 'dead', claim_token_hash = NULL,
                    claim_expires_at = NULL, last_error = 'delivery exhausted or principal revoked',
                    updated_at = clock_timestamp()
                WHERE id = candidate.id AND tenant_id = candidate.tenant_id;
                failed_version := NULL;
                UPDATE public.research_runs_pg
                SET status = 'failed', state_version = state_version + 1,
                    updated_at = clock_timestamp()
                WHERE id = (candidate.payload_json::jsonb ->> 'run_id')
                  AND tenant_id = candidate.tenant_id
                  AND status IN ('running', 'pause_requested', 'resuming')
                RETURNING state_version INTO failed_version;
                IF failed_version IS NOT NULL THEN
                    DELETE FROM public.research_leases_pg
                    WHERE run_id = (candidate.payload_json::jsonb ->> 'run_id')
                      AND tenant_id = candidate.tenant_id;
                    INSERT INTO public.research_events_pg(
                        id, run_id, tenant_id, event_type, payload_json, created_at
                    ) VALUES (
                        md5(random()::text || clock_timestamp()::text),
                        (candidate.payload_json::jsonb ->> 'run_id'), candidate.tenant_id,
                        'run.failed', '{"reason":"job_delivery_exhausted"}', clock_timestamp()
                    );
                END IF;
                reconciled := reconciled + 1;
            END LOOP;
            RETURN reconciled;
        END
        $function$
        """
    )
    op.execute("ALTER FUNCTION public.finscope_due_job_deliveries(integer) OWNER TO finscope_security_owner")
    op.execute("ALTER FUNCTION public.finscope_reconcile_exhausted_jobs(integer) OWNER TO finscope_security_owner")
    op.execute("REVOKE ALL ON FUNCTION public.finscope_reconcile_exhausted_jobs(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.finscope_due_job_deliveries(integer) TO finscope_worker")
    op.execute("GRANT EXECUTE ON FUNCTION public.finscope_reconcile_exhausted_jobs(integer) TO finscope_worker")
    op.execute("GRANT UPDATE ON jobs, research_runs_pg TO finscope_security_owner")
    op.execute("GRANT DELETE ON research_leases_pg TO finscope_security_owner")
    op.execute("GRANT INSERT ON research_events_pg TO finscope_security_owner")
    op.execute("GRANT SELECT ON research_runs_pg TO finscope_security_owner")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS public.finscope_reconcile_exhausted_jobs(integer)")
    op.execute("REVOKE UPDATE ON jobs, research_runs_pg FROM finscope_security_owner")
    op.execute("REVOKE DELETE ON research_leases_pg FROM finscope_security_owner")
    op.execute("REVOKE INSERT ON research_events_pg FROM finscope_security_owner")
    op.execute("REVOKE SELECT ON research_runs_pg FROM finscope_security_owner")
    # Restore the exact 0008 delivery semantics. 0009 owns this function through
    # finscope_security_owner, so a downgrade must not leave 0010 broker behavior behind.
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
    op.execute("ALTER FUNCTION public.finscope_due_job_deliveries(integer) OWNER TO finscope_security_owner")
    op.execute("REVOKE ALL ON FUNCTION public.finscope_due_job_deliveries(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.finscope_due_job_deliveries(integer) TO finscope_worker")
