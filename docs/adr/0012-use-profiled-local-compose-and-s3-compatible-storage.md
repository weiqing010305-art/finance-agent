# ADR-0012: Use Profiled Local Compose and S3-Compatible Storage

## Status

Accepted — 2026-08-13

## Context

The enterprise-shaped deployment must run locally first with no cloud-server cost.
The complete stack contains optional, memory-heavy Milvus and telemetry services.

## Decision

- Use Docker Compose with `core`, `rag` and `observability` profiles.
- `core` runs Caddy, static frontend, FastAPI, workers, PostgreSQL, Redis, MinIO
  and Mailpit. `rag` adds Milvus. `observability` adds OTel, Prometheus, Grafana,
  and Loki.
- Caddy is the only browser entry and applies local HTTPS and security controls.
- Store private objects in MinIO through an S3-compatible abstraction; PostgreSQL
  stores metadata, hashes and opaque keys.
- Use a shared versioned Milvus collection. PostgreSQL/RLS first returns allowed
  document IDs; Milvus only ranks the authorized set.
- Stateful services use internal networks; admin UIs bind localhost.
- Store credentials in Docker Secrets/local ignored files, not committed `.env`.

## Consequences

### Positive

- The architecture is reproducible without cloud cost.
- Profiles keep ordinary development resource use manageable.
- S3 and SMTP abstractions can move to hosted services later.

### Negative

- Docker Desktop and sufficient local memory are prerequisites.
- Local Compose is not highly available.
- Real Milvus/BGE remains an explicit environment-backed gate.

## Alternatives Considered

- All services always on: rejected due to memory cost.
- Per-tenant Milvus collections: rejected due to lifecycle complexity.
- Filesystem or PostgreSQL blobs: rejected due to weak migration boundaries.

## References

- [Phase 6 design](../plans/2026-08-13-phase-6-local-production-design.md)
