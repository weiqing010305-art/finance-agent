# Phase 5 Long-Term Memory and Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a scoped, versioned, evidence-backed long-term memory lifecycle with TTL, conflict handling, safe context injection, two-stage deletion, and reliability/security evaluations.

**Architecture:** SQLite remains the memory source of truth. Repository transactions enforce lifecycle edges, scope, evidence, idempotency, TTL, tombstones and deletion fencing; optional Milvus memory indexing is derived and can only rank candidates that already passed relational authorization filters.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite WAL, optional Milvus, pytest.

---

### Task 1: Add schema v12 and strict memory models

**Files:**
- Modify: `backend/migrations.py`
- Modify: `backend/schemas.py`
- Create: `tests/test_memory_migrations.py`

1. Write failing fresh v12, real v11→v12, repeated migration, malformed rollback, future schema, FK and CHECK tests.
2. Add `memory_records`, `memory_versions`, `memory_evidence`, `memory_events`, `memory_deletion_jobs` with tenant/user/case/company scope columns and legal states.
3. Add strict Pydantic `MemoryScope`, `MemoryCandidate`, `MemoryView`, `MemoryContextItem`, `DeletionJob` models.
4. Verify a failed v12 migration leaves max version at 11.

### Task 2: Implement candidate, verification and lifecycle transactions

**Files:**
- Create: `backend/memory.py`
- Modify: `backend/database.py`
- Create: `tests/test_memory_lifecycle.py`

1. Write tests for company fact Evidence/Claim requirements, explicit preference, summary cursor and unsupported types.
2. Add stable memory keys, canonical content hashes and typed TTL policy.
3. Implement candidate creation, deterministic re-verification at persistence, candidate→verified→active and rejection.
4. Enforce legal transitions and immutable version content in Repository.
5. Test idempotency identity, changed request conflict and fail-closed bypass attempts.

### Task 3: Implement deduplication, conflict and supersede

**Files:**
- Modify: `backend/memory.py`
- Modify: `backend/database.py`
- Create: `tests/test_memory_conflicts.py`

1. Test identical fact evidence merge and TTL refresh.
2. Test same-period differing value moves old/new versions to conflicted and prevents injection.
3. Test newer period or stronger verified replacement creates active version and supersedes old.
4. Test explicit preference correction immediately supersedes previous active version.
5. Add concurrent writers/CAS tests proving at most one active version per memory record.

### Task 4: Implement expiration and cleanup coordination

**Files:**
- Create: `backend/memory_jobs.py`
- Modify: `backend/database.py`
- Create: `tests/test_memory_expiration.py`

1. Add deterministic expiration scan and active→expired transition with events.
2. Implement claim-token/expiry fencing for background consolidation and deletion jobs.
3. Test timezone normalization, boundary instants, stale worker fencing and restart reclaim.
4. Ensure expired memory remains auditable but unreadable.

### Task 5: Implement scoped retrieval and Context Builder injection

**Files:**
- Create: `backend/memory_retrieval.py`
- Modify: `backend/context_builder.py`
- Create: `tests/test_memory_retrieval.py`
- Modify: `tests/test_context_builder.py`

1. Require explicit principal and scope on every memory query.
2. Filter active/not-deleted/not-expired records in SQLite before optional similarity ranking.
3. Rank by exact entity/type/period, freshness, confidence and evidence quality; cap at 8 items/2000 chars.
4. Inject structured envelopes after current run/case context, never raw hidden prompts.
5. Test cross-tenant/user/case leakage, prompt-injection text, stale facts and current-question priority.

### Task 6: Implement memory user-control APIs

**Files:**
- Modify: `backend/app.py`
- Modify: `backend/schemas.py`
- Create: `tests/test_memory_api.py`

1. Add local/default principal dependency that must later be replaced by Phase 6 auth.
2. Add list, explicit preference create/correct, single delete, clear-private-memory and deletion-status endpoints.
3. Reject clients attempting to create verified company facts directly.
4. Test 404/403/409, idempotency, scope isolation and response redaction.

### Task 7: Implement two-stage deletion and derived-index cleanup

**Files:**
- Modify: `backend/memory_jobs.py`
- Modify: `backend/database.py`
- Create: `tests/test_memory_deletion.py`

1. Tombstone and stop reads in the request transaction.
2. Delete only private target relations, exact Milvus IDs and cache keys under a fenced job.
3. Preserve anonymous audit hash without deleted content.
4. Test worker crash, retry, old-token fencing, shared public-fact unlink and clear-my-memory boundaries.

### Task 8: Add report-to-memory consolidation

**Files:**
- Create: `backend/memory_consolidation.py`
- Modify: `backend/app.py`
- Create: `tests/test_memory_consolidation.py`

1. Generate candidates only from persisted supported claims/evidence after report completion.
2. Apply the same Repository verifier; no model output can directly mark active.
3. Make consolidation idempotent by report/claim/version identity and crash recoverable.
4. Test conflicts, expired sources, partial claims and prompt-injection evidence.

### Task 9: Add hardening and fixed evaluations

**Files:**
- Create: `evals/memory-cases.json`
- Create: `evals/run_phase5_evals.py`
- Create: `tests/test_phase5_evals.py`
- Create: `docs/reviews/phase-5-verification.md`

1. Measure candidate acceptance, conflict accuracy, expired injection rate, cross-scope leakage, deletion correctness, retrieval precision and token budget.
2. Add failure injection for DB unavailable, stale job, duplicate writer, deletion failure and unavailable derived index.
3. Run focused tests, full pytest, compileall, Phase 2–5 evals and diff-check.
4. Record exact numbers and distinguish offline smoke from real Milvus metrics.
5. Ask the independent subagent for read-only Phase 5 review, fix all blockers and stop at user acceptance.
