# Phase 8 Formal Real-RAG Worker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Connect PostgreSQL-authorized real BGE/Milvus hybrid retrieval to the durable formal worker and complete a cited local-fixture report safely.

**Architecture:** A dedicated RAG worker image executes a persisted `real_rag_local` plan. PostgreSQL supplies bounded allowed chunk IDs, Milvus performs retrieval only, and existing durable/artifact transactions persist observations, extractive claims and the report. Job/run leases receive a shared heartbeat.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/PostgreSQL RLS, Dramatiq/Redis, sentence-transformers, PyMilvus/Milvus, Docker Compose, pytest.

---

### Task 1: Profile-aware formal plans and runtime configuration

**Files:**
- Modify: `backend/formal_research_api.py`
- Modify: `backend/formal_app.py`
- Modify: `backend/settings.py`
- Test: `tests/test_formal_research_api.py`
- Test: `tests/test_settings.py`

Add a strict `synthetic_smoke | real_rag_local` setting. Persist a minimal
retrieve-then-synthesize plan and matching job kind for the real profile. GET,
resume, retry and health must report the profile persisted in the plan rather
than a module constant.

### Task 2: Fenced long-job heartbeat and step replay

**Files:**
- Modify: `backend/db/durable.py`
- Modify: `backend/jobs/executor.py`
- Modify: `backend/jobs/ledger.py`
- Test: `tests/test_jobs_and_rate_limit.py`
- Test: `tests/test_worker_capability_handoff.py`

Add lease renewal and completed-step reads. During handler execution renew the
job claim and run lease on a bounded interval. Test loss of either fence,
long-running success, crash recovery, replay and pause-after-retrieval.

### Task 3: Real authorized RAG processor

**Files:**
- Modify: `backend/formal_processor.py`
- Modify: `backend/authorized_retrieval.py`
- Modify: `backend/milvus_retrieval.py`
- Test: `tests/test_authorized_retrieval.py`
- Create: `tests/test_formal_real_rag_processor.py`

Implement the `real_rag_local` handler. Read/replay the persisted retrieval
observation, require PostgreSQL-authorized IDs, make exact extractive claims,
persist evidence, and complete a deterministic cited report. Reject empty,
private, changed or low-authority evidence and never synthesize unsupported text.

### Task 4: Idempotent administrative fixture indexer

**Files:**
- Create: `backend/db/rag_catalog.py`
- Create: `scripts/seed_local_rag.py`
- Create: `evals/formal-rag-fixture.json`
- Test: `tests/test_postgres_rag_catalog.py`
- Test: `tests/integration/test_formal_real_rag.py`

Index a versioned labelled fixture with real BGE, upsert its UUID-safe persistent
application collection, then register exact authorization rows in PostgreSQL.
Replay must verify identity; conflicts fail closed. Add cleanup restricted to the
fixture version and an end-to-end formal job test.

### Task 5: Dedicated image, real Compose drill and phase review

**Files:**
- Modify: `Dockerfile`
- Modify: `compose.yaml`
- Modify: `scripts/local.ps1`
- Modify: `README.md`
- Modify: `docs/architecture/durable-research-agent.md`
- Create: `docs/reviews/phase-8-verification.md`

Build only the worker/indexer with RAG dependencies and a persistent model cache.
Run the synthetic default regression, then switch explicitly to
`real_rag_local`, seed, create/pause/resume/complete a real retrieval run, inspect
citations and prove cross-tenant filtering. Run the full suite/evals/compile/
dependency/Compose gates, request independent subagent review, fix every P0/P1,
and stop for user acceptance.
