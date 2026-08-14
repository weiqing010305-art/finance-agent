# Phase 6 Local Production Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a reproducible local multi-user deployment with PostgreSQL/RLS,
secure authentication, durable workers, S3-compatible files, profiled Compose,
observability and tested backup/restore.

**Architecture:** Preserve the modular monolith and Durable Runner. Add a formal
PostgreSQL Repository using SQLAlchemy Core/Alembic, defense-in-depth RLS, MinIO
object storage and PostgreSQL-ledger Dramatiq jobs. Caddy is the browser edge;
Milvus and telemetry remain optional profiles.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 Core, Alembic, PostgreSQL 17,
Argon2id, JWT, Redis, Dramatiq, MinIO/S3, Milvus, Caddy, OpenTelemetry, Prometheus,
Grafana, Loki, Docker Compose, pytest.

---

### Task 1: Runtime configuration and profiled Compose skeleton

Create `compose.yaml`, service Dockerfiles, `infra/caddy/Caddyfile`,
`backend/settings.py`, secret-init scripts and `tests/test_settings.py`. Test
missing-secret failure, profile parsing, health checks, internal networks,
localhost admin bindings, Caddy API/SSE/static routing and Compose config for each
profile.

### Task 2: PostgreSQL schema, Alembic and transaction principal

Create `alembic.ini`, `alembic/`, `backend/db/`, migration and RLS integration
tests. Define identity/tenant tables and tenant keys on private resources. Enable
and force RLS, implement `SET LOCAL` principal context and test fresh/repeat/
rollback/future migrations plus direct SQL, missing context and forged tenant.

### Task 3: Authentication and invitation onboarding

Create `backend/auth/`, auth APIs, bootstrap CLI, Mailpit SMTP adapter and tests.
Implement Argon2id, invitation create/accept/revoke, 15-minute access tokens,
seven-day refresh rotation, family replay revocation and secret-safe emails.

### Task 4: Principal context, RBAC and tenant-scoped APIs

Create centralized Policy Engine/dependencies. Encode owner/member/viewer, require
`PrincipalContext` at private boundaries, remove fixed `local/default`, revalidate
workers and test cross-tenant IDs, viewer writes and enumeration resistance.

### Task 5: Port Durable Runner semantics to PostgreSQL

Create PostgreSQL repositories and contract/integration tests for runner,
checkpoint, lease, plan, evidence, report and memory. Port one aggregate at a time,
retain CAS rowcount guards, test real concurrent transactions, and reject SQLite
URLs in formal runtime.

### Task 6: Redis throttling and Dramatiq ledger workers

Create job/outbox/DLQ schema, `backend/jobs/`, worker entrypoint and rate limiter.
Publish job ID only after commit; implement claim/heartbeat/fencing/retry/reconcile
and owner retry. Test duplicate delivery, broker loss, stale worker and Redis
fail-closed behavior.

### Task 7: MinIO quarantine and private object lifecycle

Create `backend/object_store.py`, upload/download APIs and workers. Add opaque keys,
presigned upload, byte/MIME/size/hash verification, promotion, re-authorized
download and tombstone deletion. Test spoofing, cross-tenant and crash retry.

### Task 8: Authorized Milvus multi-tenant retrieval

Query PostgreSQL/RLS for allowed IDs before Milvus, require IDs in expressions,
add versioned collection alias migration and test public sharing/private isolation.
Keep real integration explicitly skipped when Milvus is unavailable.

### Task 9: OpenTelemetry and operational dashboards

Instrument API, graph, worker, tools, RAG and reporting. Propagate trace context
without user credentials. Add latency/queue/retry/lease/quota/cost metrics,
structured log privacy tests and provisioned dashboards/alerts.

### Task 10: Backup, restore and retention

Create `scripts/backup/`, retention actors and recovery tests. Generate encrypted
full/incremental manifests with hashes, rebuild Milvus, restore into isolation and
verify counts, hashes, login and RLS. Record measured RPO/RTO.

### Task 11: Static frontend deployment and operational docs

Add production frontend build, README, architecture diagrams, seed/run scripts and
`docs/reviews/phase-6-verification.md`. Document one-command profiles, secrets,
bootstrap, key rotation and incidents. Run full tests/evals, Compose/integration
gates, request independent subagent review, fix P0/P1 and stop for user approval.

## Implementation status (2026-08-14)

- Tasks 1-11 have code/contract coverage; the current Alembic head is
  `0011_auth_role_hardening`.
- Formal `POST /api/research` atomically persists the run, actual smoke plan,
  checkpoint, initial lease, job and outbox. Redis receives only the job ID.
- A worker must hold a live PostgreSQL job claim before it can exchange that
  capability for a fenced run lease. Current membership capability is checked
  again after claim. Pause/resume uses a new durable delivery job.
- A separate least-privilege dispatcher automatically republishes unpublished,
  retry-due and stale-claim jobs. Its SECURITY DEFINER boundary returns only job
  and principal IDs, never payloads or credentials.
- The browser uses an HttpOnly/Secure/SameSite refresh cookie, keeps the 15-minute
  access token in memory, and revokes the refresh family on logout. Redis rate
  limits authentication, research writes and upload writes fail closed.
- The local default executor is deliberately `synthetic_smoke`. It exercises
  step/checkpoint/evidence/claim/citation/report transactions and is labelled as
  non-research in API, plan, evidence and report output. The proposed external
  plan is stored separately and is not represented as executed.
- The real `core` Compose profile was rebuilt from fresh volumes and passed
  bootstrap/login/research, Mailpit invitation acceptance, least-privilege role
  checks, presigned object I/O, 0010→0011 plus head→0008→head migration paths,
  and an isolated PostgreSQL+MinIO restore drill. The small local drill measured
  5.0 s and used an exact object key/size/SHA-256 inventory from the dump snapshot.
- Real external tool execution and real Milvus/BGE validation remain release
  gates. The optional hourly backup task is not installed by default, so its RPO
  is a configured target rather than a production measurement.
- Independent-review regression result: `401 passed, 1 skipped`; the skipped test is the
  environment-gated real Milvus integration.
