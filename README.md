# FinScope — Durable Finance Research Agent

FinScope is a portfolio-grade financial research agent built around explicit
intent routing, deterministic planning, a six-state durable runner, evidence
verification, hybrid RAG and governed memory. Phase 6 adds a local,
production-shaped deployment with PostgreSQL RLS, invitation authentication,
Redis/Dramatiq, MinIO, Caddy and observability profiles. Phase 8 connects the
pinned Chinese BGE model and real Milvus BM25+dense hybrid retrieval to the
durable formal worker through a PostgreSQL authorization boundary.

## Current truth

The safe default remains the clearly labelled `synthetic_smoke` executor. An
explicit `real_rag_local` profile now runs real BGE query embeddings and native
Milvus hybrid retrieval over a labelled local fixture, with allowed chunk IDs,
content hashes and authority tiers owned by PostgreSQL. It produces an
extractive cited report and supports durable pause/resume, but it still does
**not** call live financial sources or an LLM and is not investment research.

## Start locally (Windows)

Prerequisites: Docker Desktop with the Linux engine running, PowerShell and the
repository Python virtual environment.

```powershell
.\scripts\local.ps1 init
.\scripts\local.ps1 up
.\scripts\local.ps1 bootstrap -Email owner@example.com -TenantName "FinScope Local"
```

The bootstrap command prompts for a password and prints the generated tenant ID.
Open `https://localhost:8443/`, accept Caddy's local certificate warning, then log
in with that tenant ID, email and password. Mailpit is available at
`http://127.0.0.1:8025/`.

Useful commands:

```powershell
.\scripts\local.ps1 status
.\scripts\local.ps1 logs
.\scripts\local.ps1 test
.\scripts\local.ps1 down
```

Run the opt-in formal real-RAG demo after bootstrap (use the printed IDs):

```powershell
.\scripts\local.ps1 up-rag
.\scripts\local.ps1 seed-rag -TenantId <tenant-id> -UserId <user-id>
```

The first run downloads the pinned BGE model into a persistent Docker volume.
Repeated seeding verifies the same content identity and is idempotent; changed
content or authority under an existing chunk ID is rejected.

A completed run exposes its evidence as a bibliography through
`GET /api/research/{run_id}/evidence`, resolving each citation to its source
title, URL, publisher and excerpt. Two-account end-to-end isolation is verified
with `scripts/verify_formal_evidence_isolation.py` (see below).

Create an encrypted PostgreSQL + MinIO backup and run an isolated restore drill:

```powershell
.\scripts\backup_formal.ps1 create -BundlePath backups\manual.fsbk
.\scripts\backup_formal.ps1 drill -BundlePath backups\manual.fsbk
.\scripts\install_backup_schedule.ps1
```

The optional scheduled task creates an hourly encrypted full backup. Failed
research runs can be retried as a new durable run through
`POST /api/research/{run_id}/retry` with a new `Idempotency-Key`.

`down` preserves PostgreSQL, Redis, MinIO and Caddy volumes. Secrets are generated
under the ignored `secrets/` directory and are never passed in command-line URLs.

## Runtime profiles

- `core`: PostgreSQL, Redis, MinIO, Mailpit, API, worker, dispatcher and Caddy.
- `rag`: Milvus 2.6.2 with dedicated etcd/object storage.
- `rag-admin`: one-shot labelled fixture indexer; the RAG worker and indexer use
  a dedicated CPU-only image and shared persistent Hugging Face cache.
- `observability`: OpenTelemetry Collector, Prometheus, Loki and Grafana.

```powershell
docker compose --profile core --profile rag --profile observability up -d --build
```

Run the destructive-safe real retrieval gate (it creates and removes a unique
test collection and never touches the application collection):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-rag.txt
docker compose --profile rag up -d milvus-etcd milvus-minio milvus
.\.venv\Scripts\python.exe -m scripts.verify_real_rag
```

Verify two-account end-to-end API isolation (owner reads, the other tenant gets
`404`) with credentials for two bootstrap-created tenants:

```powershell
$env:FINSCOPE_A_EMAIL = "a@example.com"; $env:FINSCOPE_A_PASSWORD = "..."
$env:FINSCOPE_A_TENANT = "<tenant-a>"
$env:FINSCOPE_B_EMAIL = "b@example.com"; $env:FINSCOPE_B_PASSWORD = "..."
$env:FINSCOPE_B_TENANT = "<tenant-b>"
.\.venv\Scripts\python.exe -m scripts.verify_formal_evidence_isolation
```

## Architecture and evidence

- [Durable agent architecture](docs/architecture/durable-research-agent.md)
- [Phase 6 design](docs/plans/2026-08-13-phase-6-local-production-design.md)
- [Phase 6 implementation plan](docs/plans/2026-08-13-phase-6-local-production.md)
- [Phase 7 real Milvus/BGE verification](docs/reviews/phase-7-verification.md)
- [Phase 8 formal real-RAG verification](docs/reviews/phase-8-verification.md)
- [Architecture decisions](docs/adr/README.md)

Phase 6 acceptance covers the real `core` Compose profile and an isolated
PostgreSQL/MinIO restore drill. Phase 7 separately executed real BGE/Milvus on a
small synthetic Chinese corpus; its perfect smoke metrics do not establish
production-corpus retrieval quality, capacity or financial correctness. Phase 8
adds the end-to-end formal worker proof without claiming live finance research.
