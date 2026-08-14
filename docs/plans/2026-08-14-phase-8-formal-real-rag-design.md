# Phase 8 Formal Real-RAG Worker Design

## Scope and acceptance

Phase 8 connects the already verified Milvus/BGE path to the durable formal
worker without adding web search, filings APIs or an LLM. It must run a real
query embedding, PostgreSQL-authorized Milvus hybrid retrieval, extractive claim
verification and cited report completion inside the existing job/run state
machine. The first end-to-end proof uses an explicitly labelled local fixture;
it is not investment research.

## Alternatives

1. **Dedicated RAG worker build target (selected).** Keeps the modular monolith,
   PostgreSQL transaction model and current job queue while limiting large model
   dependencies to worker/indexer images.
2. **Internal retrieval HTTP service.** Better independent GPU scaling, but adds
   service authentication, request fencing and another deployment before scale
   data exists.
3. **Host-side retrieval bridge.** Smallest code change but not production-shaped
   and would make Docker jobs depend on an unmanaged host process.

## Data and control flow

```text
formal API -> persisted real_rag_local plan + outbox job
  -> worker job claim -> fenced run lease + dual heartbeat
  -> PostgreSQL/RLS authorization catalog -> allowed chunk IDs
  -> BGE query vector -> Milvus BM25+dense native hybrid + RRF
  -> atomic retrieval step/checkpoint
  -> extractive Evidence/Claim identities -> verifier/persistence
  -> cited deterministic report -> atomic completed state
```

An administrative one-shot indexer embeds a versioned fixture with the same
pinned BGE profile, upserts Milvus first, then records byte-identical chunk
identities in PostgreSQL. A Milvus-only orphan is unauthorized and harmless;
replay verifies identity before returning success.

## Failure, pause and recovery

- Retrieval output is persisted before report work. Recovery reads the completed
  step rather than calling Milvus again.
- A pause request observed at step commit reaches `paused`; resume replays the
  exact step and continues from evidence/report persistence.
- Job and run leases are renewed together. Heartbeat loss prevents commit and
  the durable dispatcher handles retry/dead-letter semantics.
- Empty authorized retrieval fails with a non-secret reason; it cannot fabricate
  a cited report.
- Evidence must be extractive, linked to an allowed chunk and pass authority,
  identity and citation checks already enforced by PostgreSQL artifacts.

## Non-goals

No external finance tools, LLM synthesis, PDF parser, production corpus quality
claim, GPU performance claim, reranker or multi-agent split is included.
