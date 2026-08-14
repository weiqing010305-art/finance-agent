# Phase 5 Verification

Status: PASS for code/offline scope; independent subagent review passed.

## Scope

Phase 5 introduces schema v13 and a governed long-term-memory ledger: strict write whitelist,
evidence-backed company facts, explicit user preferences and entity identity,
versioned conflict handling, TTL, relational scope filtering, structured context
injection, user APIs, tombstone-first deletion, fenced cleanup jobs and report
consolidation.

## Fixed policy

- Company research facts expire after 90 days.
- Entity identity expires after 180 days.
- Case summaries expire after 30 days.
- Task experience would expire after 90 days, but automatic writes are currently
  fail-closed until a persisted execution-summary contract exists.
- User preferences remain until changed or deleted.
- Candidate/conflict resolution jobs must be resolved within 7 days.
- Public facts are shared only by canonical company scope. User/case memory is
  tenant/user/case isolated.
- SQLite is authoritative. Milvus is a derived ranker and never grants scope.

## Safety guarantees tested

- source claims/evidence are re-read and deterministically re-verified at write;
- version content and legal state edges are protected by SQLite triggers;
- at most one active version exists per memory record;
- equal content merges; conflicting content stops injection; newer/stronger or
  explicitly corrected content supersedes;
- expired and tombstoned memory is immediately excluded from reads;
- deletion cleanup is claim-token/expiry fenced and does not return tokens via API;
- retrieval is authorized in SQLite before ranking, capped at 8 items/2000 chars,
  and projected as `untrusted_memory` structured data;
- report consolidation accepts only persisted, supported, publicly cited claims.

## Validation commands

- Independent full suite: `313 passed, 1 skipped`.
- Phase 5 focused tests are included in the full suite.
- `compileall`: passed.
- `git diff --check`: passed; line-ending conversion warnings only.
- Phase 5 eval: scope leakage `0.0`, retrieval precision smoke `1.0`, token
  budget pass `true`.

The Phase 5 eval is explicitly an offline SQLite contract smoke, not a Milvus
relevance or production-load benchmark. The single skipped test remains the real
Milvus integration gate documented in Phase 4.

## Independent adversarial review

The review reproduced and required fixes for direct DB activation bypass,
cross-company fact binding, forged case summaries, deletion-job cross-principal
access, retained private bodies, stale Claim/Evidence verification, forged
period/confidence, unresolved-conflict bypass, seven-day conflict expiry, v13
migration compatibility and unconstrained task-experience writes. The final
review found no remaining P0/P1 blocker in the code/offline scope.

## Known limitations

- Local demo uses the fixed principal `tenant_id=local,user_id=default`; real auth
  and principal derivation belong to Phase 6.
- No memory-specific Milvus/cache index is created in this phase. Deletion removes
  the SQLite private body and relationships. A future derived index requires an
  outbox/worker cleanup contract before it may be enabled.
- Context Builder exposes a structured `long_term_memory` field with an explicit
  untrusted-data marker. No LLM prompt serializer consumes that field yet, so this
  phase does not claim end-to-end prompt-injection isolation inside a model call.
- Background job scheduling is represented by fenced repository operations and an
  explicit processing endpoint; a production queue/worker belongs to Phase 6.
