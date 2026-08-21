from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, MetaData, String, Table, Text, UniqueConstraint,
)


metadata = MetaData()
users = Table(
    "users", metadata,
    Column("id", String(64), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
tenants = Table(
    "tenants", metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
memberships = Table(
    "memberships", metadata,
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role", String(16), nullable=False),
    CheckConstraint("role IN ('owner','member','viewer')", name="ck_membership_role"),
)
invitations = Table(
    "invitations", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("email", String(320), nullable=False),
    Column("role", String(16), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("accepted_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    CheckConstraint("role IN ('owner','member','viewer')", name="ck_invitation_role"),
)
refresh_tokens = Table(
    "refresh_tokens", metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("family_id", String(64), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    UniqueConstraint("family_id", "token_hash"),
)

# First tenant-owned aggregate used to prove application scoping and PostgreSQL
# RLS before the legacy durable aggregates are ported in Task 5.
tenant_resources = Table(
    "tenant_resources", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("owner_user_id", ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

jobs = Table(
    "jobs", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("created_by", ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempt", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="3"),
    Column("claim_token_hash", String(64)),
    Column("claim_expires_at", DateTime(timezone=True)),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('pending','running','retry','completed','dead')", name="ck_job_status"),
    CheckConstraint("attempt >= 0 AND max_attempts > 0", name="ck_job_attempts"),
    UniqueConstraint("id", "tenant_id", name="uq_jobs_id_tenant"),
)
job_outbox = Table(
    "job_outbox", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("published_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["job_id", "tenant_id"], ["jobs.id", "jobs.tenant_id"],
                         ondelete="CASCADE", name="fk_job_outbox_job_tenant"),
)
objects = Table(
    "objects", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("owner_user_id", ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("quarantine_key", String(512), nullable=False, unique=True),
    Column("object_key", String(512), unique=True),
    Column("declared_mime", String(128), nullable=False),
    Column("verified_mime", String(128)),
    Column("declared_size", BigInteger, nullable=False),
    Column("verified_size", BigInteger),
    Column("sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
    CheckConstraint("status IN ('pending','quarantined','ready','rejected','tombstoned','deleted')", name="ck_object_status"),
    CheckConstraint("declared_size > 0", name="ck_object_declared_size"),
)
retrieval_chunks = Table(
    "retrieval_chunks", metadata,
    Column("chunk_id", String(128), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("document_id", String(128), nullable=False),
    Column("document_version_id", String(128), nullable=False),
    Column("access_scope", String(16), nullable=False),
    Column("embedding_profile_id", String(128), nullable=False),
    Column("index_version", String(128), nullable=False),
    Column("content_hash", String(64)),
    Column("authority_tier", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("access_scope IN ('public','private')", name="ck_retrieval_chunk_scope"),
    CheckConstraint("authority_tier BETWEEN 0 AND 5", name="ck_retrieval_chunk_authority"),
)
research_runs_pg = Table(
    "research_runs_pg", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("created_by", ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_fingerprint", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("state_version", Integer, nullable=False, server_default="0"),
    Column("company", String(200), nullable=False),
    Column("question", Text, nullable=False),
    Column("progress", Integer, nullable=False, server_default="0"),
    Column("budget_used", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('running','pause_requested','paused','resuming','failed','completed')", name="ck_research_run_pg_status"),
    CheckConstraint("progress BETWEEN 0 AND 100 AND budget_used >= 0", name="ck_research_run_pg_progress"),
    UniqueConstraint("tenant_id", "created_by", "idempotency_key", name="uq_research_run_pg_idempotency"),
    UniqueConstraint("id", "tenant_id", name="uq_research_runs_pg_id_tenant"),
)
research_plans_pg = Table(
    "research_plans_pg", metadata,
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("plan_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_research_plans_run_tenant"),
)
research_checkpoints_pg = Table(
    "research_checkpoints_pg", metadata,
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("next_pointer", String(128), nullable=False),
    Column("state_json", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_research_checkpoints_run_tenant"),
)
research_leases_pg = Table(
    "research_leases_pg", metadata,
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("owner_id", String(128), nullable=False),
    Column("token_hash", String(64), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_research_leases_run_tenant"),
)
research_steps_pg = Table(
    "research_steps_pg", metadata,
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
    Column("step_id", String(128), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("input_json", Text, nullable=False),
    Column("output_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_research_steps_run_tenant"),
)
research_events_pg = Table(
    "research_events_pg", metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_research_events_run_tenant"),
)
execution_authorizations_pg = Table(
    "execution_authorizations_pg", metadata,
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
    Column("plan_version", Integer, primary_key=True),
    Column("step_id", String(128), primary_key=True),
    Column("tool_name", String(128), nullable=False),
    Column("decision", String(16), nullable=False),
    Column("reason_codes_json", Text, nullable=False),
    Column("estimated_cost", Integer, nullable=False),
    Column("budget_before", Integer, nullable=False),
    Column("effective_cost", Integer, nullable=False),
    Column("capability_token", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_exec_auth_run_tenant"),
)

evidence_items_pg = Table(
    "evidence_items_pg", metadata,
    Column("id", String(128), primary_key=True),
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("excerpt", Text, nullable=False),
    Column("source_uri", Text, nullable=False),
    Column("source_title", Text, nullable=True),
    Column("publisher", Text, nullable=True),
    Column("authority_tier", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("authority_tier BETWEEN 0 AND 5", name="ck_evidence_pg_authority"),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_evidence_items_run_tenant"),
)
claims_pg = Table(
    "claims_pg", metadata,
    Column("id", String(128), primary_key=True),
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("claim_text", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("confidence", Integer, nullable=False),
    Column("evidence_ids_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('supported','insufficient','contradicted')", name="ck_claim_pg_status"),
    CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_claim_pg_confidence"),
    UniqueConstraint("id", "tenant_id", name="uq_claims_pg_id_tenant"),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_claims_run_tenant"),
)
reports_pg = Table(
    "reports_pg", metadata,
    Column("run_id", ForeignKey("research_runs_pg.id", ondelete="CASCADE"), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("markdown", Text, nullable=False),
    Column("report_json", Text, nullable=False),
    Column("citations_json", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         ondelete="CASCADE", name="fk_reports_run_tenant"),
)
memory_records_pg = Table(
    "memory_records_pg", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("memory_type", String(32), nullable=False),
    Column("memory_key", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("content_json", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("source_run_id", ForeignKey("research_runs_pg.id", ondelete="SET NULL")),
    Column("source_claim_id", ForeignKey("claims_pg.id", ondelete="SET NULL")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("memory_type IN ('user_preference','company_fact','entity_identity')", name="ck_memory_pg_type"),
    CheckConstraint("status IN ('active','expired','tombstoned')", name="ck_memory_pg_status"),
    UniqueConstraint("tenant_id", "user_id", "memory_type", "memory_key", name="uq_memory_pg_key"),
    ForeignKeyConstraint(["source_run_id", "tenant_id"], ["research_runs_pg.id", "research_runs_pg.tenant_id"],
                         name="fk_memory_source_run_tenant"),
    ForeignKeyConstraint(["source_claim_id", "tenant_id"], ["claims_pg.id", "claims_pg.tenant_id"],
                         name="fk_memory_source_claim_tenant"),
)
audit_events_pg = Table(
    "audit_events_pg", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    Column("actor_user_id", ForeignKey("users.id", ondelete="SET NULL")),
    Column("action", String(128), nullable=False),
    Column("target_type", String(64), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

Index("ix_memberships_user", memberships.c.user_id)
Index("ix_invitations_tenant_email", invitations.c.tenant_id, invitations.c.email)
Index("ix_refresh_tokens_family", refresh_tokens.c.family_id)
Index("ix_tenant_resources_tenant", tenant_resources.c.tenant_id)
Index("ix_jobs_due", jobs.c.status, jobs.c.next_attempt_at)
Index("ix_jobs_tenant", jobs.c.tenant_id)
Index("ix_job_outbox_unpublished", job_outbox.c.published_at)
Index("ix_objects_tenant_status", objects.c.tenant_id, objects.c.status)
Index(
    "ix_retrieval_chunks_authorization", retrieval_chunks.c.tenant_id,
    retrieval_chunks.c.access_scope, retrieval_chunks.c.embedding_profile_id,
    retrieval_chunks.c.index_version,
)
Index("ix_research_runs_pg_tenant_status", research_runs_pg.c.tenant_id, research_runs_pg.c.status)
Index("ix_research_events_pg_run", research_events_pg.c.tenant_id, research_events_pg.c.run_id)
Index("ix_evidence_items_pg_run", evidence_items_pg.c.tenant_id, evidence_items_pg.c.run_id)
Index("ix_claims_pg_run", claims_pg.c.tenant_id, claims_pg.c.run_id)
Index("ix_memory_records_pg_expiry", memory_records_pg.c.tenant_id, memory_records_pg.c.status, memory_records_pg.c.expires_at)
Index("ix_audit_events_pg_expiry", audit_events_pg.c.expires_at)
