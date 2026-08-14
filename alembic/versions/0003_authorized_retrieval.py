"""Add PostgreSQL authorization catalog for shared Milvus chunks."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_authorized_retrieval"
down_revision = "0002_jobs_and_objects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retrieval_chunks", sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("document_version_id", sa.String(128), nullable=False),
        sa.Column("access_scope", sa.String(16), nullable=False),
        sa.Column("embedding_profile_id", sa.String(128), nullable=False),
        sa.Column("index_version", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("access_scope IN ('public','private')", name="ck_retrieval_chunk_scope"),
    )
    op.create_index(
        "ix_retrieval_chunks_authorization", "retrieval_chunks",
        ["tenant_id", "access_scope", "embedding_profile_id", "index_version"],
    )
    op.execute(sa.text('ALTER TABLE "retrieval_chunks" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE "retrieval_chunks" FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(
        'CREATE POLICY "retrieval_chunks_select" ON "retrieval_chunks" FOR SELECT '
        "USING (access_scope = 'public' OR tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))"
    ))
    op.execute(sa.text(
        'CREATE POLICY "retrieval_chunks_modify" ON "retrieval_chunks" FOR ALL '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))"
    ))


def downgrade() -> None:
    op.drop_table("retrieval_chunks")
