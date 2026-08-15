# Phase 8 Verification — Formal Real-RAG Worker

Status: **PASS — real local gates and independent adversarial review passed.**

## Scope

- Dedicated `rag-runtime` worker/indexer image with pinned CPU-only
  `torch 2.13.0+cpu`, `sentence-transformers 5.7.0` and `pymilvus 2.6.17`.
- Persistent Hugging Face cache and fixed
  `BAAI/bge-large-zh-v1.5@79e7739b6ab944e86d6171e44d24c997fc1e0116` profile.
- Alembic `0012_retrieval_identity_fencing`: PostgreSQL owns allowed chunk IDs,
  content hashes and authority tiers; pre-0012 rows remain unauthorized until
  explicitly re-indexed.
- Idempotent labelled fixture seed/delete command and deterministic extractive,
  cited formal report processor with job/run heartbeat and completed-step replay.

## Real execution evidence (2026-08-15)

- Existing PostgreSQL upgraded `0011 -> 0012`; API reports
  `research_executor=real_rag_local` and Milvus 2.6.2 is healthy.
- First Docker seed downloaded the pinned model, embedded five fixture chunks and
  upserted `finance_agent_chunks_v1`; a second identical seed passed with exactly
  five PostgreSQL catalog rows, proving idempotent replay.
- Formal API/Redis/Dramatiq worker completed a real RAG run with four persisted
  citations. A second run was created while the worker was stopped, requested
  pause, reached `paused` after retrieval commit, resumed and completed with the
  same four cited identities.
- Worker-role cross-tenant test: owning tenant retrieved one private marker;
  another tenant retrieved three public rows and zero private markers.
- Real `0012 -> 0011 -> 0012` migration roundtrip passed; matching legacy rows
  were explicitly re-indexed from unauthorized `content_hash=NULL` to five
  fenced identities. An encrypted 0012 PostgreSQL/MinIO backup was then created
  and passed an isolated restore drill in `5.1s`.
- The worker started only after loading the pinned model and validating the
  existing Milvus schema, BM25 function, 1024-dimensional dense field and both
  indexes.
- After the final identity-filter and atomic-heartbeat fixes, the API,
  dispatcher and worker images were rebuilt and a fresh real run again passed
  `pause_requested -> paused -> resuming -> completed` with four citations.

## Regression gates

- Full pytest: `420 passed, 3 skipped, 1 warning` in `29.26s`. The skips are
  explicit external integration gates; their real Milvus/BGE and formal
  tenant-isolation equivalents were executed separately above.
- Phase 3/4/5 offline evals pass; Phase 4 correctly reports
  `real_milvus_executed=false` because its default eval remains an in-memory smoke.
- `compileall`, Compose configuration and `git diff --check` pass (line-ending
  warnings only).

## Commands

```powershell
.\scripts\local.ps1 up-rag
.\scripts\local.ps1 bootstrap -Email owner@example.com -TenantName "FinScope Local"
.\scripts\local.ps1 seed-rag -TenantId <tenant-id> -UserId <user-id>
$env:FINSCOPE_SMOKE_EMAIL = "owner@example.com"
$env:FINSCOPE_SMOKE_PASSWORD = "<local password>"
$env:FINSCOPE_SMOKE_TENANT = "<tenant-id>"
.\.venv\Scripts\python.exe -m scripts.verify_formal_real_rag
```

## Explicit limitations

- Fixture content is synthetic and explicitly labelled. No live financial source,
  web tool or LLM is called, so this is not current investment research.
- Five chunks do not establish production Recall, reranking quality, capacity,
  concurrency or GPU latency. CPU correctness is the only model-runtime claim.
- The application collection is persistent and is not deleted by the gate; the
  administrative delete command is restricted to fixture document versions.
- Milvus is a rebuildable index, not an authorization or audit source.

## Follow-up: bibliography endpoint and two-account isolation (2026-08-15)

Two previously open Phase 8 items are now resolved:

- `GET /api/research/{run_id}/evidence` resolves each persisted evidence item to
  its source title, URL, publisher, excerpt, authority tier and supporting
  claims, through the same PostgreSQL/RLS read boundary as the report. Titles and
  publishers are persisted by Alembic `0013_evidence_bibliography` and populated
  by both the synthetic smoke and real RAG processors.
- `scripts/verify_formal_evidence_isolation.py` demonstrates two-account,
  end-to-end API isolation over real HTTP: the owning tenant reads its run and
  evidence while a second tenant receives `404` for both. A SQLite
  TestClient-based isolation test runs in CI without external services.

Full pytest: `425 passed, 3 skipped` (five new tests).
