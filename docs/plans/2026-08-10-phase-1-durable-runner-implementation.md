# Phase 1 Durable Runner Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the one-shot task persistence prototype with a six-state SQLite-backed runner that atomically persists execution progress, supports safe pause/resume, and recovers expired runs without duplicating committed work.

**Architecture:** A versioned native-SQLite migration layer owns the schema. `Repository` remains the persistence boundary, while a new `DurableRunner` owns legal state transitions, lease tokens, CAS versions, and checkpoint commits. FastAPI and research workers call the runner rather than writing run state directly; SSE continues to replay persisted events.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, FastAPI, Pydantic, asyncio, pytest.

---

### Task 1: Freeze executable state contracts in tests

**Files:**
- Create: `tests/test_durable_runner.py`
- Modify: `backend/schemas.py`

**Steps:**

1. Write failing tests asserting the exact six run states and legal transitions.
2. Add cases for duplicate pause/resume, immutable terminal states, and illegal transitions returning domain conflicts.
3. Run `python -m pytest tests/test_durable_runner.py -q` and confirm failure because the runner does not exist.
4. Add state enums and typed transition errors without changing API behavior yet.
5. Re-run the focused tests.

### Task 2: Add versioned SQLite migrations

**Files:**
- Create: `backend/migrations.py`
- Modify: `backend/database.py`
- Test: `tests/test_database_migrations.py`

**Steps:**

1. Write a failing fresh-database schema test for `schema_migrations`, `agent_runs`, `plans`, `run_steps`, `tool_calls`, `checkpoints`, `run_leases`, `events`, and existing evidence/case data.
2. Write a failing upgrade test that creates the old `tasks` schema, inserts rows/events/evidence, calls `initialize()`, and verifies preserved records plus deterministic status mapping (`queued -> running`, legacy `cancelled -> failed` with migration reason).
3. Implement ordered, transactional migration functions and record the applied version.
4. Ensure foreign keys, WAL, indexes, unique idempotency keys, and one lease row per run.
5. Run migration tests twice to prove idempotent initialization.

### Task 3: Implement atomic run creation and initial lease

**Files:**
- Create: `backend/durable_runner.py`
- Modify: `backend/database.py`
- Test: `tests/test_durable_runner.py`

**Steps:**

1. Test `create_run()` creates run, initial plan/checkpoint/lease/event in one transaction.
2. Test repeating the same idempotency key returns the original run and does not create another lease/event.
3. Test lease tokens are returned only by the internal runner result and absent from API views/events.
4. Implement repository transaction and `DurableRunner.create_run()`.
5. Run focused tests.

### Task 4: Implement CAS transitions and lease rules

**Files:**
- Modify: `backend/durable_runner.py`
- Modify: `backend/database.py`
- Test: `tests/test_durable_runner.py`

**Steps:**

1. Test `running -> pause_requested -> paused -> resuming -> running` and exact persisted event order.
2. Test stale `state_version`, wrong lease token, expired lease, and competing resume owner.
3. Test lease renewal and takeover only after expiry.
4. Implement CAS transition helpers, safe-pause acknowledgement, resume verification, renewal, and typed conflicts.
5. Run focused tests including two repository instances against one database.

### Task 5: Implement atomic step/tool/checkpoint commit

**Files:**
- Modify: `backend/durable_runner.py`
- Modify: `backend/database.py`
- Test: `tests/test_durable_runner.py`

**Steps:**

1. Test a single transaction writes step input/output, tool result, budget delta, evidence payload, next frontier, checkpoint, and event.
2. Inject a failure before commit and assert no partial rows or frontier movement remain.
3. Repeat an idempotency key and assert the successful tool/step is not duplicated.
4. Test a pause request racing with step completion: result persists, no new step is claimed, and run reaches a safe pause.
5. Implement the minimal transaction and read models.

### Task 6: Integrate FastAPI and research workers

**Files:**
- Modify: `backend/app.py`
- Modify: `backend/mock_research.py`
- Modify: `backend/research.py`
- Modify: `backend/schemas.py`
- Test: `tests/test_api.py`

**Steps:**

1. Update API tests to expect create=`running`, pause=`pause_requested` then eventually `paused`, resume=`resuming` then `running`, and no new `cancelled` writes.
2. Replace the cancel endpoint with a compatibility response that maps the old stop action to safe pause until the cancelled ADR is decided.
3. Pass internal lease tokens into workers; workers checkpoint each completed stage through the runner.
4. Make long DeepSeek calls honor pause at the next safe boundary and never claim a new stage while pause is requested.
5. Preserve existing TaskView fields and evidence/report behavior for the frontend.

### Task 7: Add startup reconciliation and SSE replay tests

**Files:**
- Modify: `backend/durable_runner.py`
- Modify: `backend/app.py`
- Test: `tests/test_recovery.py`
- Test: `tests/test_api.py`

**Steps:**

1. Create runs with expired/missing leases and assert startup reconciler acquires them with CAS and validates the last checkpoint.
2. Assert committed steps are not rerun after simulated process restart.
3. Assert corrupt/incomplete checkpoints become recoverable failure records when the database is writable.
4. Assert SSE `Last-Event-ID` replays only later persisted events, including `run.resuming`, `run.running`, and terminal events.
5. Ensure lifespan shutdown releases/cancels only in-process tasks without corrupting persisted runs.

### Task 8: Verify and document Phase 1

**Files:**
- Modify: `docs/architecture/durable-research-agent.md`
- Modify: `docs/adr/0002-adopt-six-state-durable-runner.md`
- Create: `docs/reviews/phase-1-verification.md`

**Steps:**

1. Run focused migration, runner, recovery, API, frontend-contract, and eval tests.
2. Run the entire suite with `python -m pytest -q`.
3. Record implemented versus deferred capabilities, test counts, known limits, and exact recovery semantics.
4. Ask an independent subagent to review code, tests, migrations, concurrency invariants, and architecture consistency.
5. Fix findings, rerun the suite, request subagent re-review, and only then ask the user to approve Phase 1.
