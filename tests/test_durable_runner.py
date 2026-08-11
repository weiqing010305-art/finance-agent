from __future__ import annotations

from datetime import timedelta
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner, RunConflict, SIX_RUN_STATES
from backend.schemas import ResearchCreate


def request() -> ResearchCreate:
    return ResearchCreate(
        company="腾讯控股",
        symbol="0700.HK",
        market="HK",
        question="分析腾讯近三年的盈利质量",
    )


@pytest.fixture
def runner(tmp_path):
    repository = Repository(tmp_path / "runner.db")
    repository.initialize()
    return DurableRunner(repository, lease_ttl=timedelta(seconds=30))


def test_contract_exposes_exactly_six_run_states():
    assert SIX_RUN_STATES == {
        "running",
        "pause_requested",
        "paused",
        "resuming",
        "failed",
        "completed",
    }


def test_create_run_atomically_creates_plan_checkpoint_lease_and_event(runner):
    created = runner.create_run(
        request(),
        owner_id="worker-a",
        idempotency_key="create-1",
    )

    run = created.run
    assert run["status"] == "running"
    assert run["state_version"] == 1
    assert created.lease_token
    snapshot = runner.repository.get_runtime_snapshot(run["id"])
    assert snapshot["plan"]["version"] == 1
    assert snapshot["checkpoint"]["sequence"] == 0
    assert snapshot["lease"]["owner_id"] == "worker-a"
    assert [event["kind"] for event in snapshot["events"]] == ["run.started"]
    assert "lease_token" not in run
    assert "lease_token" not in snapshot["events"][0].get("payload", {})


def test_create_run_is_idempotent_and_does_not_rotate_lease(runner):
    first = runner.create_run(request(), owner_id="worker-a", idempotency_key="same-key")
    second = runner.create_run(request(), owner_id="worker-b", idempotency_key="same-key")

    assert second.run["id"] == first.run["id"]
    assert second.lease_token == first.lease_token
    snapshot = runner.repository.get_runtime_snapshot(first.run["id"])
    assert snapshot["lease"]["owner_id"] == "worker-a"
    assert len(snapshot["events"]) == 1


def test_six_state_pause_resume_path_and_duplicate_commands(runner):
    created = runner.create_run(request(), owner_id="worker-a", idempotency_key="flow")
    run_id = created.run["id"]

    requested = runner.request_pause(run_id)
    duplicate = runner.request_pause(run_id)
    assert requested["status"] == duplicate["status"] == "pause_requested"
    assert requested["state_version"] == duplicate["state_version"]

    paused = runner.acknowledge_pause(run_id, lease_token=created.lease_token)
    assert paused["status"] == "paused"
    assert runner.request_pause(run_id)["status"] == "paused"

    resuming = runner.request_resume(run_id, owner_id="worker-a")
    duplicate_resume = runner.request_resume(run_id, owner_id="worker-a")
    assert resuming["status"] == duplicate_resume["status"] == "resuming"

    running = runner.finish_resume(run_id, lease_token=resuming["lease_token"])
    assert running["status"] == "running"

    kinds = [item["kind"] for item in runner.repository.list_events(run_id)]
    assert kinds == [
        "run.started",
        "run.pause_requested",
        "run.paused",
        "run.resuming",
        "run.running",
    ]


def test_terminal_states_are_immutable(runner):
    created = runner.create_run(request(), owner_id="worker-a", idempotency_key="terminal")
    failed = runner.fail_run(
        created.run["id"],
        lease_token=created.lease_token,
        error="boom",
    )
    assert failed["status"] == "failed"

    with pytest.raises(RunConflict):
        runner.request_pause(created.run["id"])
    with pytest.raises(RunConflict):
        runner.request_resume(created.run["id"], owner_id="worker-a")


def test_terminal_execution_fields_are_immutable_in_database(runner):
    completed = runner.create_run(
        request(), owner_id="worker-a", idempotency_key="completed-fields"
    )
    runner.complete_run(
        completed.run["id"],
        lease_token=completed.lease_token,
        result={"title": "final"},
        evidence=[],
    )
    with pytest.raises(sqlite3.IntegrityError, match="completed run execution fields"):
        with runner.repository.connect() as connection:
            connection.execute(
                "UPDATE agent_runs SET result_json = '{}' WHERE id = ?",
                (completed.run["id"],),
            )

    failed = runner.create_run(
        request(), owner_id="worker-a", idempotency_key="failed-fields"
    )
    runner.fail_run(failed.run["id"], lease_token=failed.lease_token, error="boom")
    with pytest.raises(sqlite3.IntegrityError, match="failed run execution fields"):
        with runner.repository.connect() as connection:
            connection.execute(
                "UPDATE agent_runs SET error = 'rewritten' WHERE id = ?",
                (failed.run["id"],),
            )


def test_wrong_lease_and_stale_state_version_are_rejected(runner):
    created = runner.create_run(request(), owner_id="worker-a", idempotency_key="guards")
    run_id = created.run["id"]
    runner.request_pause(run_id)

    with pytest.raises(RunConflict, match="lease token"):
        runner.acknowledge_pause(run_id, lease_token="wrong")
    with pytest.raises(ValueError, match="stale state version"):
        runner.repository.cas_transition(
            run_id,
            from_statuses=("pause_requested",),
            to_status="paused",
            kind="run.paused",
            message="stale",
            expected_version=1,
            lease_token=created.lease_token,
        )


def test_lease_can_be_renewed_but_only_taken_over_after_expiry(tmp_path):
    repository = Repository(tmp_path / "lease.db")
    repository.initialize()
    runner = DurableRunner(repository, lease_ttl=timedelta(milliseconds=50))
    created = runner.create_run(request(), owner_id="worker-a", idempotency_key="lease")

    renewed = runner.renew_lease(created.run["id"], lease_token=created.lease_token)
    assert renewed["owner_id"] == "worker-a"
    with pytest.raises(RunConflict, match="active lease"):
        runner.take_over_expired_run(created.run["id"], owner_id="worker-b")

    with repository.connect() as connection:
        connection.execute(
            "UPDATE run_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (created.run["id"],),
        )
    takeover = runner.take_over_expired_run(created.run["id"], owner_id="worker-b")
    assert takeover["run"]["status"] == "resuming"
    assert takeover["lease_token"] != created.lease_token


def test_step_tool_and_checkpoint_commit_is_atomic_and_idempotent(runner):
    created = runner.create_run(request(), owner_id="worker-a", idempotency_key="step")
    run_id = created.run["id"]
    frontier = {
        "plan_version": 1,
        "ready_step_ids": ["s2"],
        "running_step_ids": [],
        "blocked_step_ids": [],
        "completed_step_ids": ["s1"],
    }
    tool = {
        "name": "search_filings",
        "version": "1",
        "input": {"query": "腾讯财报"},
        "output": {"hits": 3},
        "duration_ms": 12,
        "cost_units": 2,
        "idempotency_key": "tool-s1",
    }
    committed = runner.commit_step(
        run_id,
        lease_token=created.lease_token,
        step_id="s1",
        kind="search",
        step_input={"query": "腾讯财报"},
        step_output={"hits": 3},
        idempotency_key="step-s1",
        frontier=frontier,
        progress=30,
        budget_delta=2,
        tool=tool,
    )
    duplicate = runner.commit_step(
        run_id,
        lease_token=created.lease_token,
        step_id="s1",
        kind="search",
        step_input={"query": "腾讯财报"},
        step_output={"hits": 3},
        idempotency_key="step-s1",
        frontier=frontier,
        progress=30,
        budget_delta=2,
        tool=tool,
    )

    assert committed["budget_used"] == duplicate["budget_used"] == 2
    snapshot = runner.repository.get_runtime_snapshot(run_id)
    assert snapshot["checkpoint"]["sequence"] == 1
    assert snapshot["checkpoint"]["frontier"] == frontier
    assert snapshot["counts"] == {"steps": 1, "tool_calls": 1, "checkpoints": 2}


def test_checkpoint_failure_rolls_back_step_and_frontier(runner):
    created = runner.create_run(request(), owner_id="worker-a", idempotency_key="rollback")
    run_id = created.run["id"]
    with runner.repository.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_checkpoint BEFORE INSERT ON checkpoints
            WHEN NEW.sequence > 0 BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="checkpoint unavailable"):
        runner.commit_step(
            run_id,
            lease_token=created.lease_token,
            step_id="s1",
            kind="search",
            step_input={},
            step_output={"ok": True},
            idempotency_key="rollback-step",
            frontier={"completed_step_ids": ["s1"]},
            progress=20,
            budget_delta=1,
        )

    snapshot = runner.repository.get_runtime_snapshot(run_id)
    assert snapshot["run"]["progress"] == 0
    assert snapshot["run"]["budget_used"] == 0
    assert snapshot["checkpoint"]["sequence"] == 0
    assert snapshot["counts"]["steps"] == 0


def test_step_commit_during_pause_request_saves_result_then_pauses(runner):
    created = runner.create_run(request(), owner_id="worker-a", idempotency_key="pause-race")
    run_id = created.run["id"]
    runner.request_pause(run_id)

    paused = runner.commit_step(
        run_id,
        lease_token=created.lease_token,
        step_id="s1",
        kind="read",
        step_input={},
        step_output={"saved": True},
        idempotency_key="pause-step",
        frontier={"ready_step_ids": ["s2"], "completed_step_ids": ["s1"]},
        progress=40,
        budget_delta=1,
    )

    assert paused["status"] == "paused"
    assert runner.repository.get_runtime_snapshot(run_id)["lease"] is None
    assert [event["kind"] for event in runner.repository.list_events(run_id)][-2:] == [
        "step.completed",
        "run.paused",
    ]


def test_expired_lease_cannot_transition_or_write_runtime_data(runner):
    created = runner.create_run(request(), owner_id="old", idempotency_key="expired-writer")
    runner.request_pause(created.run["id"])
    with runner.repository.connect() as connection:
        connection.execute(
            "UPDATE run_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (created.run["id"],),
        )

    with pytest.raises(RunConflict, match="expired"):
        runner.acknowledge_pause(created.run["id"], lease_token=created.lease_token)
    with pytest.raises(PermissionError, match="expired"):
        runner.repository.append_runtime_event(
            created.run["id"], kind="provider.progress", step="x", progress=1,
            message="stale", lease_token=created.lease_token,
        )
    with pytest.raises(PermissionError, match="expired"):
        runner.repository.replace_evidence(
            created.run["id"], [], lease_token=created.lease_token
        )


def test_repository_rejects_illegal_edges_and_invalid_raw_status(runner):
    created = runner.create_run(request(), owner_id="worker", idempotency_key="edges")
    with pytest.raises(ValueError, match="illegal state edge"):
        runner.repository.cas_transition(
            created.run["id"], from_statuses=("running",), to_status="completed",
            kind="bad", message="bad",
        )
    with pytest.raises(sqlite3.IntegrityError, match="invalid agent run status"):
        with runner.repository.connect() as connection:
            connection.execute(
                "UPDATE agent_runs SET status = 'cancelled' WHERE id = ?",
                (created.run["id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="illegal agent run state transition"):
        with runner.repository.connect() as connection:
            connection.execute(
                "UPDATE agent_runs SET status = 'paused' WHERE id = ?",
                (created.run["id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="matching checkpoint"):
        with runner.repository.connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs SET status = 'completed', progress = 100,
                    result_json = '{}', state_version = state_version + 1
                WHERE id = ?
                """,
                (created.run["id"],),
            )


def test_step_idempotency_rejects_changed_payload_and_invalid_execution_values(runner):
    created = runner.create_run(request(), owner_id="worker", idempotency_key="step-guards")
    kwargs = dict(
        run_id=created.run["id"], lease_token=created.lease_token,
        step_id="s1", kind="search", step_input={"q": "a"},
        step_output={"hits": 1}, idempotency_key="same-step",
        frontier={"plan_version": 1, "completed_step_ids": ["s1"]},
        progress=10, budget_delta=1,
    )
    runner.commit_step(**kwargs)
    with pytest.raises(RunConflict, match="reused"):
        runner.commit_step(**{**kwargs, "step_output": {"hits": 2}})
    with pytest.raises(RunConflict, match="reused"):
        runner.commit_step(**{**kwargs, "frontier": {"plan_version": 1, "ready_step_ids": ["s2"]}})
    with pytest.raises(RunConflict, match="progress"):
        runner.commit_step(**{**kwargs, "idempotency_key": "bad-progress", "progress": 101})
    with pytest.raises(RunConflict, match="budget_delta"):
        runner.commit_step(**{**kwargs, "idempotency_key": "bad-budget", "budget_delta": -1})


def test_concurrent_create_with_same_key_returns_one_run_and_one_event(tmp_path):
    repository = Repository(tmp_path / "concurrent-create.db")
    repository.initialize()

    def create(owner: str):
        return DurableRunner(Repository(repository.database_path)).create_run(
            request(), owner_id=owner, idempotency_key="concurrent-key"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["a", "b"]))

    assert len({item.run["id"] for item in results}) == 1
    run_id = results[0].run["id"]
    assert [event["kind"] for event in repository.list_events(run_id)] == ["run.started"]


def test_concurrent_pause_and_takeover_never_report_a_lost_successful_pause(tmp_path):
    path = tmp_path / "pause-takeover-race.db"
    repository = Repository(path)
    repository.initialize()
    original = DurableRunner(repository)
    created = original.create_run(request(), owner_id="old", idempotency_key="race")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE run_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (created.run["id"],),
        )
    barrier = Barrier(2)

    def pause():
        barrier.wait()
        try:
            return ("ok", DurableRunner(Repository(path)).request_pause(created.run["id"]))
        except RunConflict as exc:
            return ("conflict", str(exc))

    def takeover():
        barrier.wait()
        try:
            return ("ok", DurableRunner(Repository(path)).take_over_expired_run(
                created.run["id"], owner_id="new"
            ))
        except RunConflict as exc:
            return ("conflict", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        pause_result = pool.submit(pause)
        takeover_result = pool.submit(takeover)
        pause_outcome = pause_result.result()
        takeover_outcome = takeover_result.result()

    if pause_outcome[0] == "ok" and takeover_outcome[0] == "ok":
        assert takeover_outcome[1]["previous_status"] == "pause_requested"
    else:
        assert pause_outcome[0] == "conflict" or takeover_outcome[0] == "conflict"


def test_completed_evidence_enrichment_is_versioned_audited_and_redacted(runner):
    created = runner.create_run(request(), owner_id="worker", idempotency_key="enrich")
    evidence = [{
        "citation_number": 1, "title": "报告", "publisher": "公司",
        "url": (
            "https://urluser:urlpass@example.com/report?token=secret&year=2025&"
            "client_secret=clientvalue&refresh_token=refreshvalue#access_token=fragmentvalue"
        ),
        "source_type": "一手来源", "excerpt": "摘要", "agent": "财报分析 Agent",
    }]
    runner.complete_run(
        created.run["id"], lease_token=created.lease_token,
        result={"title": "完成"}, evidence=evidence,
    )
    updated_evidence = [{**evidence[0], "title": "更新后的报告"}]
    enriched = runner.repository.enrich_completed_evidence(created.run["id"], updated_evidence)

    assert "token=secret" not in enriched["evidence"][0]["url"]
    for value in ("urluser", "urlpass", "clientvalue", "refreshvalue", "fragmentvalue"):
        assert value not in enriched["evidence"][0]["url"]
    assert "#" not in enriched["evidence"][0]["url"]
    assert "year=2025" in enriched["evidence"][0]["url"]
    snapshot = runner.repository.get_runtime_snapshot(created.run["id"])
    assert snapshot["checkpoint"]["state"]["evidence_enriched"] is True
    assert snapshot["checkpoint"]["state"]["frontier"] == snapshot["run"]["frontier"]
    assert snapshot["checkpoint"]["state"]["budget_used"] == snapshot["run"]["budget_used"]
    assert snapshot["checkpoint"]["state"]["evidence_before_hash"] != snapshot["checkpoint"]["state"]["evidence_after_hash"]
    assert snapshot["events"][-1]["kind"] == "evidence.enriched"
