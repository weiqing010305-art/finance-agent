# Phase 6 Verification

Status at Phase 6 close: **PASS for code and real `core` Compose scope; independent review passed. Real Milvus/BGE was NOT EXECUTED in this phase.** It was executed later under [Phase 7](phase-7-verification.md).

## Scope delivered

- PostgreSQL 17 schema managed by SQLAlchemy Core/Alembic; head
  `0011_auth_role_hardening`.
- Tenant/user/membership/invitation identity, fixed owner/member/viewer RBAC and
  forced RLS policies on private tables.
- Argon2id passwords, 15-minute signed access JWTs, rotating seven-day refresh
  families, replay revocation and invitation-only onboarding.
- Browser refresh token in a Secure, HttpOnly, SameSite=Strict cookie; access
  token stays in page memory; logout revokes the server-side family.
- Atomic run/plan/checkpoint/lease/job/outbox creation and six-state durable run
  operations. Broker job IDs are inert until a live job claim is exchanged for
  a separately fenced run lease.
- Separate admin, app and worker database credentials. Runtime roles are neither
  superusers nor `BYPASSRLS`, cannot assume the admin role and cannot create in
  the public schema. Composite tenant foreign keys fail closed across child rows.
- The dispatcher republishes unpublished, broker-lost, retry-due and stale-claim
  deliveries without reading payloads globally. Exhausted/revoked work is moved
  to a dead-letter state and its run is failed atomically.
- Redis fail-closed limits for authentication, research writes and uploads.
- MinIO quarantine/promote/tombstone contracts with fenced promotion keys,
  browser-reachable presigned POST/GET URLs and upload-size policy; PostgreSQL-
  authorized Milvus IDs; telemetry profiles; and encrypted/hash-manifest backup
  bundles covering PostgreSQL plus MinIO.
- Caddy HTTPS/security headers and a static formal console for login, create,
  six-state status, pause, resume and verified-report display, plus a dedicated
  one-time invitation acceptance page that clears its URL token.

## Honest execution profile

The formal worker currently runs `synthetic_smoke`. The persisted actual plan,
API response, evidence and report all identify that profile. It calls no external
financial tools. A separate proposed external plan may be inspected but is not
represented as executed. This validates orchestration and persistence only.

## Validation evidence (2026-08-14)

- Independent-review full pytest suite: `401 passed, 1 skipped, 2 warnings` in 23.30 seconds.
- The skip is the environment-gated real Milvus integration test.
- Phase 3 eval: entity accuracy `1.0`, ambiguity safety `1.0`, planner/DAG smoke
  `1.0`, no failures.
- Phase 4 offline eval: Recall/MRR/NDCG@3 `1.0`, citation coverage/integrity and
  numeric provenance `1.0`; profile explicitly `in_memory_test_smoke`,
  `real_milvus_executed=false`.
- Phase 5 offline eval: scope leakage `0.0`, retrieval precision smoke `1.0`,
  token budget pass `true`; mode explicitly `sqlite_offline_smoke`.
- `python -m compileall`: pass.
- `pip check`: no broken requirements.
- `alembic heads`: exactly `0011_auth_role_hardening`.
- Compose configuration parses for `core`, `core+rag`, and
  `core+rag+observability` profiles.
- `node --check prototype-research-ui/formal-console.js`: pass.
- Headless Edge render at 1440 x 1000: visually inspected; login layout and
  Chinese typography render correctly.
- `git diff --check`: no whitespace errors; Windows line-ending warnings only.
- PowerShell parser checks pass for local lifecycle, backup and backup-schedule
  scripts. Secret files are ignored; stateful service ports are not publicly bound.

## Adversarial boundaries covered offline

- cross-tenant lookups return no private row and viewers cannot create research;
- stale/forged job claims cannot obtain a run lease and an old lease cannot commit;
- membership capability is re-read after job creation and before execution;
- broker failure preserves an unpublished outbox record; a published-but-unclaimed
  delivery is republished after its grace period;
- expired claims are republished with delivery throttling, final-attempt crashes
  are dead-lettered and stale completion is fenced;
- replaying a committed step while pause is requested reaches `paused` rather
  than stranding the run in `pause_requested`;
- a concurrent object-promotion loser cannot delete the winner's bytes;
- access-token use immediately revalidates user and tenant membership state;
- invitation delivery through Mailpit does not expose the raw token in the formal
  HTTP response; the real email link opens the invitation page, accepts a new
  password and permits the invited user to log in;
- evidence/claim crash replay accepts only byte-identical identities, and a
  pause after evidence can resume through report completion;
- a terminal handler attempt dead-letters its job and fails its run in one transaction;
- refresh replay revokes a token family and logout revokes the active family;
- report completion requires persisted supported claims, linked evidence hashes,
  claim hashes and citation markers in one transaction;
- browser report output uses `textContent`, CSP forbids inline scripts/objects and
  no bearer/refresh token is persisted in Web Storage;
- PostgreSQL SECURITY DEFINER functions use a fixed search path, revoke PUBLIC
  execution and return identity metadata only, never job payloads.

## Real local gate

The Docker Desktop Linux engine was started and the `core` profile was rebuilt
from fresh volumes. The following gates executed successfully:

- fresh migration to `0011_auth_role_hardening`, service health and Caddy API health at
  `https://localhost:8443/api/health`;
- existing-volume 0010→0011 upgrade and a real head→0008→head round trip that
  restored the previous delivery function, ACL and user-RLS state;
- bootstrap, login and a durable `synthetic_smoke` research run reaching
  `completed` through Redis/Dramatiq/PostgreSQL;
- live SQL checks proving app/worker are non-superuser, non-`BYPASSRLS`, cannot
  assume admin and use distinct password hashes;
- real presigned browser upload to `https://localhost:9443`, verify/promote and
  authorized download with byte-for-byte equality;
- encrypted backup containing a snapshot-consistent `pg_dump` plus an exact
  key/size/SHA-256 inventory and a real 29-byte MinIO object, followed by an
  isolated temporary-database and temporary-bucket restore drill in 5.0 s.

The stack was then reset again to clean fresh volumes and left healthy at 0011, without a
bootstrap owner. The optional Windows scheduled task is not installed, so the
hourly RPO is a configured target only after the operator installs it. The 5.0 s
RTO observation is for this tiny local dataset and is not a capacity guarantee.

At Phase 6 close, the real Milvus/BGE integration was separately **NOT EXECUTED** because
`MILVUS_TEST_URI` and the real model runtime were not configured. Observability
profile configuration parses, but full signal delivery was not deeply exercised.
The console currently polls rather than consuming SSE. MinIO credentials are
separated by role but can be narrowed further with custom bucket-scoped policies.

## Independent review

Passed on 2026-08-14 with `P0=0`, `P1=0`. The reviewer independently checked
migration compatibility, auth/RLS boundaries, durable-job recovery, invitation
onboarding, frontend token handling, exact object backup inventory and the live
core deployment. Remaining P2 work: observability-only OTLP activation, narrower
MinIO prefix policies, a snapshot holder without a fixed 300-second ceiling,
dependency CVE/SBOM gating, SSE console delivery and the optional backup schedule.
These Phase 6 checks did not imply real Milvus/BGE acceptance; the later Phase 7
gate provides that separate execution evidence.
