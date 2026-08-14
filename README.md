# FinScope — Durable Finance Research Agent

FinScope is a portfolio-grade financial research agent built around explicit
intent routing, deterministic planning, a six-state durable runner, evidence
verification, hybrid RAG and governed memory. Phase 6 adds a local,
production-shaped deployment with PostgreSQL RLS, invitation authentication,
Redis/Dramatiq, MinIO, Caddy and observability profiles. Phase 7 proves the
pinned Chinese BGE model against a real Milvus BM25+dense hybrid collection.

## Current truth

The formal local runtime currently uses the clearly labelled
`synthetic_smoke` executor. It proves authentication, tenant isolation, durable
job delivery, pause/resume, checkpointing and verified report persistence. It
does **not** call external financial tools and its output must not be treated as
investment research. Real Milvus/BGE retrieval is now independently verified;
connecting it to the formal executor and external research remains a later gate.

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
- `rag`: Milvus 2.6.2 with dedicated etcd/object storage; BGE runs in the host venv.
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

## Architecture and evidence

- [Durable agent architecture](docs/architecture/durable-research-agent.md)
- [Phase 6 design](docs/plans/2026-08-13-phase-6-local-production-design.md)
- [Phase 6 implementation plan](docs/plans/2026-08-13-phase-6-local-production.md)
- [Phase 7 real Milvus/BGE verification](docs/reviews/phase-7-verification.md)
- [Architecture decisions](docs/adr/README.md)

Phase 6 acceptance covers the real `core` Compose profile and an isolated
PostgreSQL/MinIO restore drill. Phase 7 separately executed real BGE/Milvus on a
small synthetic Chinese corpus; its perfect smoke metrics do not establish
production-corpus retrieval quality, capacity or financial correctness.
