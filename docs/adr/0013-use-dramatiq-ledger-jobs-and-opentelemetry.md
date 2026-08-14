# ADR-0013: Use Dramatiq Ledger Jobs and OpenTelemetry

## Status

Accepted — 2026-08-13

## Context

Ingestion, deletion, expiry, reconciliation and backups run outside requests.
Redis delivery cannot be the business source of truth, and Agent recovery/cost
must be observable.

## Decision

- Use Redis + Dramatiq for delivery; PostgreSQL job rows remain authoritative.
- Messages contain only `job_id`; workers reload tenant, permission and state.
- Preserve claim-token, lease, heartbeat, idempotency, retry and reconciler rules.
  Exceeded retries become dead-letter state.
- Use Redis sliding-window throttles for login, API, uploads and research quotas.
  Authentication and costly writes fail closed when Redis is unavailable.
- Instrument API, graph, worker, tools, retrieval and reports with OpenTelemetry.
  Prometheus, Grafana and Loki run under an optional profile.
- Retain audit and runtime metadata for 90 days, excluding secrets/deleted body.
- Back up PostgreSQL and MinIO daily with hourly increments and an encrypted,
  hashed manifest. Treat Milvus as rebuildable. Target RPO <= 1h, RTO <= 2h.

## Consequences

### Positive

- Redis message loss can be reconciled from durable state.
- Traces explain latency, recovery and tool cost end to end.
- Restore drills validate RLS, not just backup creation.

### Negative

- Redis, workers and telemetry add operational components.
- RPO/RTO remain objectives until demonstrated by restore tests.
- A local single host cannot tolerate host loss while running.

## Alternatives Considered

- Celery: rejected as heavier than needed.
- PostgreSQL-only custom queue: rejected due to custom scheduling complexity.
- Logs only: rejected because cross-process execution needs trace correlation.

## References

- [Phase 6 design](../plans/2026-08-13-phase-6-local-production-design.md)
