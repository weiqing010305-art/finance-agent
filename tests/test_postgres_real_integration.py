"""Real-PostgreSQL integration gate: RLS tenant isolation and the durable
contract on an actual server.

The SQLite-based ``test_postgres_durable_contract`` can never exercise row
level security; this gate runs the real Alembic migrations (which
ENABLE/FORCE RLS and create the tenant policies) against a real PostgreSQL and
proves cross-tenant isolation fails closed.

Skipped unless ``FINSCOPE_TEST_PG_URL`` is set. Disposable container:

    docker run -d --name dsh-pgtest -e POSTGRES_PASSWORD=test-password \\
        -p 127.0.0.1:55432:5432 postgres:17-alpine
    $env:FINSCOPE_TEST_PG_URL="postgresql+psycopg://postgres:test-password@127.0.0.1:55432/postgres"
    pytest tests/test_postgres_real_integration.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text

REPO_ROOT = Path(__file__).resolve().parent.parent
PG_URL = os.getenv("FINSCOPE_TEST_PG_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="requires FINSCOPE_TEST_PG_URL pointing at a real PostgreSQL",
)

from alembic import command
from alembic.config import Config

from backend.auth.models import PrincipalContext
from backend.db.durable import PostgresDurableRepository
from backend.db.session import principal_transaction


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(PG_URL, pool_pre_ping=True)
    # Bring the schema to the Alembic head against the real server.
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = PG_URL
    try:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        command.upgrade(config, "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def app_engine():
    """Non-superuser connection: RLS only applies to non-superuser roles.

    The migrations GRANT the app role table privileges; row-level security
    can only be exercised through this role (superusers bypass it).
    """
    url = PG_URL.replace("postgres:test-password@", "finscope_app:test-password@", 1)
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def clean_schema(engine):
    # Wipe identity rows so each test starts empty, keep the schema.
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE tenant_resources, refresh_tokens, invitations, memberships, users, tenants CASCADE"))
    yield


def _seed(engine) -> None:
    now = "2026-01-01T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, email, password_hash, is_active, created_at) VALUES "
            "('u1','u1@example.com','x',true,:now), ('u2','u2@example.com','x',true,:now)"
        ), {"now": now})
        connection.execute(text(
            "INSERT INTO tenants (id, name, created_at) VALUES ('t1','t1',:now), ('t2','t2',:now)"
        ), {"now": now})
        connection.execute(text(
            "INSERT INTO memberships (tenant_id, user_id, role) VALUES "
            "('t1','u1','owner'), ('t2','u2','owner')"
        ))


def test_rls_isolates_tenant_resources(engine, app_engine, clean_schema):
    # Identity rows are seeded as the superuser (users RLS only permits
    # insertion via invitation); isolation is then exercised through the
    # non-superuser app role, where FORCE RLS actually applies.
    _seed(engine)
    t1 = PrincipalContext("u1", "t1", "owner")
    t2 = PrincipalContext("u2", "t2", "owner")
    with principal_transaction(app_engine, t1) as connection:
        connection.execute(text(
            "INSERT INTO tenant_resources (id, tenant_id, owner_user_id, kind, payload_json, created_at) "
            "VALUES ('r1','t1','u1','note', :payload, :created_at)"
        ), {"payload": '{"x":1}', "created_at": "2026-01-01T00:00:00+00:00"})
    # t1 sees its own row.
    with principal_transaction(app_engine, t1) as connection:
        assert connection.execute(
            text("SELECT count(*) FROM tenant_resources")
        ).scalar() == 1
    # t2 sees nothing: FORCE RLS + the tenant policy filter on app.tenant_id.
    with principal_transaction(app_engine, t2) as connection:
        assert connection.execute(
            text("SELECT count(*) FROM tenant_resources")
        ).scalar() == 0


def test_durable_contract_on_real_postgres(engine, clean_schema):
    _seed(engine)
    principal = PrincipalContext("u1", "t1", "owner")
    repo = PostgresDurableRepository(engine)
    created = repo.create_run(
        principal, company="Tencent", question="cash flow",
        idempotency_key="pg-run", plan={"steps": [{"id": "s1"}]}, owner_id="worker",
    )
    assert created.created and created.lease_token
    replay = repo.create_run(
        principal, company="Tencent", question="cash flow",
        idempotency_key="pg-run", plan={"steps": [{"id": "s1"}]}, owner_id="worker2",
    )
    assert replay.run_id == created.run_id and not replay.created
    # Cross-tenant read fails closed on the real server.
    assert repo.get_run(PrincipalContext("u2", "t2", "owner"), created.run_id) is None
    # Pause/resume cycle with CAS works against real RLS rows.
    repo.transition(principal, created.run_id, from_status="running", to_status="pause_requested", expected_version=0)
    snapshot = repo.commit_step(
        principal, created.run_id, lease_token=created.lease_token, step_id="s1",
        step_input={"q": "x"}, step_output={"answer": 1}, next_pointer="s2",
        progress=30, budget_delta=2,
    )
    assert snapshot["run"]["status"] == "paused"
    assert snapshot["checkpoint"]["next_pointer"] == "s2"
    assert repo.get_run(principal, created.run_id)["status"] == "paused"
