# Phase 4 RAG, Evidence Verification and Streaming Report Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Build a recoverable Milvus hybrid retrieval, evidence verification, and citation-constrained streaming report pipeline that safely completes Phase 3 runs.

**Architecture:** SQLite remains the transactional source of truth for document versions, ingestion jobs, evidence, claims, report snapshots, and run lifecycle. Milvus Standalone is a replaceable derived index using native BM25 sparse retrieval plus normalized `BAAI/bge-large-zh-v1.5` dense vectors and RRF; offline tests use an explicitly labelled in-memory implementation of the same interfaces.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, LangGraph, SQLite, PyMilvus, sentence-transformers/FlagEmbedding-compatible provider, Milvus Standalone, pytest.

---

### Task 1: Freeze Phase 4 schemas and migration v10

**Files:**
- Modify: `backend/migrations.py`
- Modify: `backend/schemas.py`
- Modify: `backend/database.py`
- Create: `tests/test_phase4_migrations.py`

1. Write failing tests for fresh v10, real v9→v10 data upgrade, repeated migration, malformed pre-existing table rollback, foreign keys, unique identities and CHECK constraints.
2. Add `documents`, `document_versions`, `document_chunks`, `ingestion_jobs`, `evidence_items`, `claims`, `claim_evidence`, `report_generations`, `report_snapshots`, `reports`, and `report_citations`.
3. Give document/version/chunk/index jobs stable idempotency identities and immutable content hashes.
4. Add strict Pydantic domain models for source metadata, parsed sections, chunks, evidence, claims, report drafts and final reports.
5. Run the migration tests and confirm that a failed v10 migration leaves max schema version at 9.

### Task 2: Implement deterministic document parsing and chunking

**Files:**
- Create: `backend/documents.py`
- Create: `backend/chunking.py`
- Create: `tests/test_documents.py`
- Create: `tests/fixtures/documents/`

1. Write tests for UTF-8/HTML/plain-text normalization, repeated ingestion, changed source version, empty input, size limits and prompt-injection-like text treated as data.
2. Implement immutable SHA-256 document versions and normalized section extraction.
3. Implement deterministic heading-aware chunks with stable IDs, bounded characters/tokens and overlap that never crosses document versions.
4. Persist the document/version/chunk/job transaction before indexing.
5. Verify identical input reuses the same version and changed content creates a new version without overwriting old chunks.

### Task 3: Implement embedding contracts and BGE Large provider

**Files:**
- Create: `backend/embeddings.py`
- Create: `tests/test_embeddings.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

1. Write provider contract tests for query/document separation, 1024 dimensions, normalized finite values, batching, empty input and model load failure.
2. Implement `EmbeddingProfile` with model `BAAI/bge-large-zh-v1.5`, pinned revision, dimension 1024, query instruction and normalize flag.
3. Implement lazy local BGE provider. Queries receive the retrieval instruction; documents do not.
4. Implement deterministic Hash Embedding only in the test support module; responses and logs must identify it as test-only.
5. Keep heavy model dependencies optional so ordinary unit tests do not download weights. Add a separately marked local-model integration test.

### Task 4: Implement HybridRetriever interfaces and in-memory oracle

**Files:**
- Create: `backend/retrieval.py`
- Create: `tests/test_hybrid_retrieval.py`
- Create: `evals/rag-retrieval-cases.json`

1. Define strict index/upsert/delete/search/health interfaces and typed requests/results.
2. Implement an in-memory BM25 + dense + deterministic RRF oracle for unit tests; label backend `in_memory_test`.
3. Test metadata filters, dense/sparse/fused scores, stable ties, RRF ranks, duplicate chunks, authority/freshness/diversity post-policy and version metadata.
4. Add financial retrieval cases for company names/codes, accounting terms, semantic paraphrases, periods and conflicting sources.
5. Record BM25-only, dense-only and hybrid metrics separately.

### Task 5: Implement Milvus collection, ingestion and hybrid search

**Files:**
- Create: `backend/milvus_retrieval.py`
- Create: `backend/ingestion.py`
- Create: `tests/integration/test_milvus_hybrid.py`
- Create: `scripts/milvus/README.md`
- Create: `scripts/milvus/standalone.env.example`

1. Write fake-client contract tests before importing PyMilvus in production code.
2. Define a versioned collection schema with text, sparse vector, 1024-dimensional dense vector and all required metadata/access fields.
3. Configure native BM25 Function, dense index and RRF hybrid search. Verify every success result contains embedding/index versions.
4. Implement idempotent upsert keys and an ingestion reconciler for pending/failed jobs and stale index versions.
5. Implement health checks and explicit unavailable/degraded behavior; never silently select the in-memory backend in production.
6. Add `@pytest.mark.milvus` tests that run only when `MILVUS_TEST_URI` is set. The suite must create a unique test collection and delete only that exact collection after validation.
7. Document the official Windows Docker Desktop + WSL2 startup path and port 19530 without automatically starting or deleting containers.

### Task 6: Connect the real retrieve_documents tool

**Files:**
- Modify: `backend/tool_registry.py`
- Modify: `backend/app.py`
- Create: `backend/research_tools.py`
- Create: `tests/test_research_tools.py`

1. Replace only the `retrieve_documents` unconfigured handler with a dependency-injected adapter; keep other unimplemented real tools explicitly degraded.
2. Validate tool input filters against the confirmed entity and access scope; Planner-provided filters cannot widen authorization.
3. Return typed Milvus hits, scores, versions and evidence references through the existing claim/observation/commit ledger.
4. Test Milvus unavailable, dense failure→BM25-only, sparse failure→dense-only and both failed→fail closed.
5. Ensure each fallback sets `degraded`, `degraded_reason`, `fallback_used`, and emits persisted observability events.

### Task 7: Build Evidence Pack and Claim Verifier

**Files:**
- Create: `backend/evidence.py`
- Create: `backend/verifier.py`
- Modify: `backend/database.py`
- Create: `tests/test_evidence_verifier.py`

1. Normalize retrieved/tool facts into immutable Evidence items with content hashes and access scope.
2. Extract atomic Claims from deterministic facts/report outline; every numeric Claim must carry period, unit and currency where applicable.
3. Implement deterministic support checks, source authority/freshness rules, conflict detection and citation access validation.
4. Add an optional restricted semantic verifier interface that can only select existing Evidence IDs and reason codes.
5. Persist Evidence, Claims, links and verification decisions atomically and idempotently.
6. Test unsupported numbers, conflicting reports, stale source, duplicate source, inaccessible evidence and malicious text.

### Task 8: Implement citation-constrained report generation

**Files:**
- Create: `backend/reporting.py`
- Create: `backend/report_graph.py`
- Create: `tests/test_reporting.py`

1. Define a strict `ReportDraft`/`ReportSection` schema where factual statements reference verified Claim IDs.
2. Implement a deterministic renderer used as the recovery and no-model fallback.
3. Implement a model generator adapter that only receives verified Claims/Evidence and must return known IDs; reject invented IDs, URLs and unsupported numbers.
4. Render stable Markdown/JSON citations and disclose degraded/partial/conflicted limitations.
5. Test zero evidence, partial support, conflicts, invented citation IDs, numeric mismatch and source ordering.

### Task 9: Persist report deltas and complete the run atomically

**Files:**
- Modify: `backend/database.py`
- Modify: `backend/durable_runner.py`
- Modify: `backend/app.py`
- Create: `tests/test_report_recovery.py`
- Modify: `tests/test_api.py`

1. Add repository operations that persist generation identity and cumulative report snapshots before publishing `report.delta`.
2. Resume from the latest snapshot; if model continuation cannot be proven idempotent, deterministically rebuild from verified Claims.
3. Add a final transaction that writes report, citation mapping, final checkpoint, `report.completed`, and `run.completed` together.
4. Ensure pause requests are honored between evidence verification and report snapshots.
5. Test crash after delta persistence, crash before final transaction, duplicate completion, SSE Last-Event-ID replay and terminal immutability.

### Task 10: Add Phase 4 evals, docs and review gate

**Files:**
- Create: `evals/run_phase4_evals.py`
- Create: `docs/reviews/phase-4-verification.md`
- Modify: `docs/architecture/durable-research-agent.md`
- Modify: `docs/adr/README.md`
- Modify: `README.md` only if it already exists at this point

1. Calculate Recall@k, MRR, nDCG, citation coverage/integrity, numeric provenance, conflict disclosure, degradation accuracy and recovery duplication rate.
2. Report in-memory smoke metrics separately from real Milvus integration metrics.
3. Run focused tests after every task, then full pytest, compileall, Phase 1–4 evals and `git diff --check`.
4. Record exact commands, results, known limitations, model/index versions and whether real Milvus tests were executed or skipped.
5. Ask the independent Phase 4 subagent for read-only review; fix all blockers and rerun verification.
6. Stop at the user acceptance checkpoint. Do not begin Phase 5 without explicit approval.
