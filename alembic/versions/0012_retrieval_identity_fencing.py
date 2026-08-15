"""Fence Milvus results with PostgreSQL-owned content identities."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_retrieval_identity_fencing"
down_revision = "0011_auth_role_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("retrieval_chunks", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column(
        "retrieval_chunks",
        sa.Column("authority_tier", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_retrieval_chunk_authority", "retrieval_chunks",
        "authority_tier BETWEEN 0 AND 5",
    )
    op.alter_column("retrieval_chunks", "authority_tier", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_retrieval_chunk_authority", "retrieval_chunks", type_="check")
    op.drop_column("retrieval_chunks", "authority_tier")
    op.drop_column("retrieval_chunks", "content_hash")
