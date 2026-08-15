"""Persist source titles and publishers for the bibliography/evidence endpoint."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_evidence_bibliography"
down_revision = "0012_retrieval_identity_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evidence_items_pg", sa.Column("source_title", sa.Text(), nullable=True))
    op.add_column("evidence_items_pg", sa.Column("publisher", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evidence_items_pg", "publisher")
    op.drop_column("evidence_items_pg", "source_title")
