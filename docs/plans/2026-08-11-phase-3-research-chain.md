# Phase 3 Research Chain Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Build the durable entity-resolution, DAG-planning, policy/budget, tool-registry, and execution chain for research intents.

**Architecture:** FastAPI and LangGraph orchestrate pure domain services. SQLite Repository persists intake, confirmation, plan and authorization facts; Durable Runner remains the only lifecycle/checkpoint writer. Planner and tools cannot directly mutate runtime state.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, LangGraph, LangChain messages, SQLite, pytest.

---

### Task 1: Freeze Phase 3 schemas and migrations

**Files:** `backend/schemas.py`, `backend/migrations.py`, `backend/database.py`, `tests/test_phase3_migrations.py`

1. Write failing migration tests for fresh and v6 databases.
2. Add schema v9 intake, entity confirmation, authorization reservation/history and tool claim/observation tables with indexes and CHECK constraints.
3. Add strict Pydantic entity, candidate, plan, step, authorization and API schemas.
4. Run migration/schema tests; malformed or conflicting identities must roll back.

### Task 2: Implement deterministic entity resolution

**Files:** `backend/entity_resolver.py`, `backend/securities.json`, `tests/test_entity_resolver.py`

1. Test aliases, ticker/market, same-name candidates, unknown entity and current-case correction.
2. Implement normalized catalog matching and confidence/status output.
3. Require confirmation for multiple candidates or inferred market; never silently choose an ambiguous security.
4. Run resolver tests and add a fixed resolver eval set.

### Task 3: Persist idempotent intake and confirmation

**Files:** `backend/database.py`, `tests/test_research_intake_repository.py`

1. Test request replay, changed-payload conflict, pending confirmation, expiry and concurrent single-winner resolution.
2. Implement intake creation/update and entity-confirmation Repository methods under `BEGIN IMMEDIATE`.
3. Link exactly one confirmed intake to one run; prevent post-resolution mutation.
4. Run repository concurrency and migration tests.

### Task 4: Build and validate versioned Planner DAGs

**Files:** `backend/planner.py`, `backend/schemas.py`, `backend/database.py`, `tests/test_planner.py`

1. Test dynamic plans for profitability, cash flow, valuation and risk questions.
2. Test duplicate IDs, missing dependencies, cycles, invalid retry counts and excessive estimated cost.
3. Implement deterministic fallback planner and a replaceable planner protocol.
4. Add atomic plan installation that increments version and writes matching frontier/checkpoint/event.
5. Test one replan maximum and immutable historical plan versions.

### Task 5: Implement Tool Registry and Policy/Budget Gate

**Files:** `backend/tool_registry.py`, `backend/policy.py`, `tests/test_tool_registry.py`, `tests/test_policy.py`

1. Define the six versioned ToolSpec contracts and typed errors.
2. Test unknown tool, malformed input/output, timeout, idempotency and output limits with fake handlers.
3. Test route intent, confirmed entity, risk, confirmation and budget denials.
4. Persist authorization allow/deny facts without exposing secrets.

### Task 6: Implement durable Executor and one replan

**Files:** `backend/research_executor.py`, `backend/durable_runner.py`, `tests/test_research_executor.py`

1. Test frontier calculation and dependency ordering.
2. Execute independent ready steps concurrently but commit each completed observation atomically.
3. Stop at safe checkpoint on pause; recover from completed step/tool ledgers without duplicate calls.
4. Trigger at most one replan on explicit insufficient-observation outcome; otherwise follow fallback or fail.
5. Test budget exhaustion, lease loss and partial batch failure.

### Task 7: Connect LangGraph and FastAPI

**Files:** `backend/research_graph.py`, `backend/app.py`, `backend/schemas.py`, `tests/test_research_orchestration_api.py`

1. Add orchestration endpoints to start an existing research route and resolve entity confirmation.
2. Build graph nodes `load_route -> intake -> resolve -> confirm_or_plan -> authorize -> execute`.
3. Keep `/api/conversations/route` non-destructive and legacy `/api/research` compatible.
4. Test exact entity, ambiguous entity, idempotent replay, permission denial and background execution.

### Task 8: Evaluate, document and independently review

**Files:** `evals/entity-resolution-cases.json`, `evals/planner-cases.json`, `docs/reviews/phase-3-verification.md`, `docs/architecture/durable-research-agent.md`

1. Run focused tests, full pytest, compileall and diff-check.
2. Record resolver accuracy, ambiguity safety, DAG validity, permission bypass rate, budget adherence and duplicate tool execution rate.
3. Run failure injection for every ToolSpec fallback and persistence boundary.
4. Ask an independent subagent for a read-only Phase 3 review, fix all blockers, rerun verification, then request user acceptance before Phase 4.
