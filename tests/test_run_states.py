"""Tests for the state-machine single source of truth, SQL splitter and
lease-takeover grace period (P0 debt fixes)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.database import LEGAL_TRANSITIONS, Repository
from backend.db.durable import LEGAL_EDGES
from backend.durable_runner import DurableRunner, RunConflict
from backend.migrations import _split_sql_statements
from backend.run_states import RECOVERY_TRANSITIONS, RUN_STATE_TRANSITIONS
from backend.schemas import ResearchCreate


def request() -> ResearchCreate:
    return ResearchCreate(company="腾讯控股", question="分析腾讯近三年的盈利质量")


def iso(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)).isoformat()


def expire_lease(repository: Repository, run_id: str, expires_at_iso: str) -> None:
    with repository.connect() as connection:
        connection.execute(
            "UPDATE run_leases SET expires_at = ? WHERE run_id = ?",
            (expires_at_iso, run_id),
        )


# ---------------------------------------------------------------------------
# 1. State machine rules have a single source of truth
# ---------------------------------------------------------------------------


def test_transition_sets_are_consistent_across_repositories():
    # The SQLite CAS guard and the PostgreSQL guard must both derive from the
    # shared constant (the drift that dropped running->completed is fixed).
    assert LEGAL_TRANSITIONS == RUN_STATE_TRANSITIONS
    assert LEGAL_EDGES == RUN_STATE_TRANSITIONS
    assert ("running", "completed") in RUN_STATE_TRANSITIONS


def test_recovery_edges_are_separate():
    assert ("running", "resuming") in RECOVERY_TRANSITIONS
    assert ("pause_requested", "resuming") in RECOVERY_TRANSITIONS
    assert ("running", "resuming") not in RUN_STATE_TRANSITIONS


# ---------------------------------------------------------------------------
# 2. SQL splitting is aware of string literals and comments
# ---------------------------------------------------------------------------


def test_split_sql_statements_handles_semicolons_in_strings():
    script = (
        "INSERT INTO t VALUES ('含;分号');"
        "INSERT INTO t VALUES ('escaped ''quote'' ; still one');"
        "UPDATE t SET x = 1;"
    )
    statements = _split_sql_statements(script)
    assert len(statements) == 3
    assert "含;分号" in statements[0]
    assert "escaped ''quote'' ; still one" in statements[1]
    assert statements[2] == "UPDATE t SET x = 1"


def test_split_sql_statements_ignores_comment_semicolons():
    script = (
        "-- 注释里有;分号\n"
        "CREATE TABLE x (id INTEGER);\n"
        "/* 块注释 ; */\n"
        "INSERT INTO x VALUES (1);"
    )
    statements = _split_sql_statements(script)
    # Two statements: comments stay attached to their statement, and the
    # semicolons inside them do not break the split.
    assert len(statements) == 2
    assert statements[0].startswith("-- 注释里有;分号")
    assert "CREATE TABLE x (id INTEGER)" in statements[0]
    assert statements[1].startswith("/* 块注释 ; */")
    assert statements[1].endswith("INSERT INTO x VALUES (1)")


def test_split_sql_statements_empty_and_whitespace():
    assert _split_sql_statements("") == []
    assert _split_sql_statements("  ;  ;  ") == []
    assert _split_sql_statements("SELECT 1") == ["SELECT 1"]


# ---------------------------------------------------------------------------
# 3. Lease takeover respects a grace period
# ---------------------------------------------------------------------------


def test_takeover_within_grace_period_is_rejected(tmp_path):
    repository = Repository(tmp_path / "grace.db")
    repository.initialize()
    runner = DurableRunner(repository, lease_ttl=timedelta(seconds=30))
    created = runner.create_run(request(), owner_id="old", idempotency_key="grace")
    expire_lease(repository, created.run["id"], iso(offset_seconds=2))  # 2s ago

    with pytest.raises(RunConflict, match="grace"):
        runner.take_over_expired_run(created.run["id"], owner_id="new", grace_seconds=10)
    # The run still belongs to the old owner.
    assert repository.get_task(created.run["id"])["status"] == "running"


def test_takeover_after_grace_period_succeeds(tmp_path):
    repository = Repository(tmp_path / "grace-ok.db")
    repository.initialize()
    runner = DurableRunner(repository, lease_ttl=timedelta(seconds=30))
    created = runner.create_run(request(), owner_id="old", idempotency_key="grace-ok")
    expire_lease(repository, created.run["id"], "2000-01-01T00:00:00+00:00")

    takeover = runner.take_over_expired_run(created.run["id"], owner_id="new", grace_seconds=10)
    assert takeover["run"]["status"] == "resuming"
    assert takeover["lease_token"] != created.lease_token


def test_takeover_default_grace_is_zero(tmp_path):
    # Backwards compatibility: explicit calls without a grace argument keep the
    # old behaviour (any expired lease is claimable immediately).
    repository = Repository(tmp_path / "grace-zero.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(request(), owner_id="old", idempotency_key="grace-zero")
    expire_lease(repository, created.run["id"], iso(offset_seconds=1))

    takeover = runner.take_over_expired_run(created.run["id"], owner_id="new")
    assert takeover["run"]["status"] == "resuming"


def test_reconciler_skips_runs_within_grace(tmp_path):
    repository = Repository(tmp_path / "grace-reconcile.db")
    repository.initialize()
    runner = DurableRunner(repository, lease_ttl=timedelta(seconds=30))  # grace = 10s
    created = runner.create_run(request(), owner_id="old", idempotency_key="grace-rec")
    expire_lease(repository, created.run["id"], iso(offset_seconds=2))  # within grace

    recovered = runner.reconcile_expired_runs(owner_id="new")
    assert recovered == []
    assert repository.get_task(created.run["id"])["status"] == "running"


def test_reconciler_recovers_after_grace(tmp_path):
    repository = Repository(tmp_path / "grace-recover.db")
    repository.initialize()
    runner = DurableRunner(repository, lease_ttl=timedelta(seconds=30))
    created = runner.create_run(request(), owner_id="old", idempotency_key="grace-rec2")
    expire_lease(repository, created.run["id"], "2000-01-01T00:00:00+00:00")

    recovered = runner.reconcile_expired_runs(owner_id="new")
    assert len(recovered) == 1
    assert recovered[0].run["status"] == "running"
