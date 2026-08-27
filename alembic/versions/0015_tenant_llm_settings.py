"""Per-tenant LLM settings store (model / api key / base URL)."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_tenant_llm_settings"
down_revision = "0014_execution_authorizations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_llm_settings_pg",
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="deepseek"),
        sa.Column("model", sa.String(64), nullable=False, server_default="deepseek-v4-flash"),
        sa.Column("base_url", sa.String(255), nullable=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_llm_settings_pg TO finscope_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_llm_settings_pg TO finscope_worker")


def downgrade() -> None:
    op.execute("REVOKE ALL PRIVILEGES ON tenant_llm_settings_pg FROM finscope_app")
    op.execute("REVOKE ALL PRIVILEGES ON tenant_llm_settings_pg FROM finscope_worker")
    op.drop_table("tenant_llm_settings_pg")