# ADR-0014: Run authorized RAG in a dedicated worker image

Status: Proposed

Date: 2026-08-14

## Context

Phase 7 proved real BGE and Milvus on the host, while the formal PostgreSQL job
worker still executes `synthetic_smoke`. Query embeddings are required at job
execution time. Installing the RAG stack in every API/admin image would enlarge
the whole deployment; a new HTTP retrieval service would add authentication,
network capability and lifecycle complexity before scale requires it.

The PostgreSQL `retrieval_chunks` catalog is the authorization source. Milvus is
a rebuildable index and must never decide which tenant may retrieve a chunk.
Long model work also exceeds the original 30-second synthetic-worker lease.

## Decision

Use a dedicated Docker build target for the formal worker. It contains the
pinned RAG dependencies and shares a persistent Hugging Face cache with an
explicit one-shot administrative indexer. API, dispatcher and migration images
remain model-free.

The `real_rag_local` processor obtains allowed chunk IDs from PostgreSQL/RLS,
passes only that bounded capability set to Milvus, persists the exact retrieval
observation before synthesis, emits extractive claims only, and completes the
report through the existing evidence/citation transaction. A background fenced
heartbeat renews both the PostgreSQL job claim and run lease; losing either makes
subsequent persistence fail closed.

`synthetic_smoke` remains an explicit configuration fallback. The local fixture
profile is never labelled external financial research.

## Consequences

- The worker image and first startup are larger/slower, but API/admin images stay
  small and the model cache is persistent.
- PostgreSQL remains the authorization and audit authority; direct Milvus access
  is not exposed to clients.
- A one-shot seed/index command is required before the local fixture executor can
  produce evidence.
- A separate retrieval service remains an option only when independent scaling
  or GPU scheduling is justified by measurements.
