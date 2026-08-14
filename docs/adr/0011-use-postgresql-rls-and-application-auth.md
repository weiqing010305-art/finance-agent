# ADR-0011: Use PostgreSQL RLS and Application Authentication

## Status

Accepted — 2026-08-13

## Context

The local demo uses SQLite and a fixed `local/default` principal. Phase 6 must
demonstrate real multi-user isolation without maintaining two production database
semantics. The runtime also depends on explicit transactions, leases and CAS.

## Decision

- PostgreSQL is the only formal runtime database; SQLite remains a unit-test
  adapter and read-only historical artifact. Existing SQLite data is not migrated.
- Use SQLAlchemy 2 Core and Alembic with explicit transaction boundaries.
- Every private resource is tenant-bound. API/Repository checks and PostgreSQL RLS
  both enforce isolation; missing transaction principal context fails closed.
- Use organizations, memberships and fixed `owner/member/viewer` roles.
- Use invitation-only onboarding, Argon2id, 15-minute access tokens and rotating
  seven-day refresh-token families. Persist only token hashes.
- Keep authentication and policy interfaces replaceable by OIDC later.

## Consequences

### Positive

- Repository omissions do not automatically become cross-tenant disclosure.
- Transaction and concurrency semantics match the intended deployment.
- The authorization model is small enough to test exhaustively.

### Negative

- PostgreSQL integration tests are mandatory and slower than SQLite tests.
- Custom authentication requires careful token rotation, throttling and auditing.
- Existing SQLite demo data is unavailable in the new runtime.

## Alternatives Considered

- Dual SQLite/PostgreSQL runtime: rejected due to divergent locking semantics.
- Keycloak/OIDC now: rejected as too operationally heavy for local deployment.
- Application-only tenant filtering: rejected because one missing predicate leaks.

## References

- [Phase 6 design](../plans/2026-08-13-phase-6-local-production-design.md)
