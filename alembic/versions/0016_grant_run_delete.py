"""Grant DELETE on research_runs_pg to the formal API role."""
from __future__ import annotations

from alembic import op


revision = "0016_grant_run_delete"
down_revision = "0015_tenant_llm_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # finscope_app powers the delete endpoint in formal_app.
    op.execute("GRANT DELETE ON research_runs_pg TO finscope_app")
    # Keep the worker able to delete rows as part of job recovery if needed.
    op.execute("GRANT DELETE ON research_runs_pg TO finscope_worker")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON research_runs_pg FROM finscope_worker")
    op.execute("REVOKE DELETE ON research_runs_pg FROM finscope_app")
