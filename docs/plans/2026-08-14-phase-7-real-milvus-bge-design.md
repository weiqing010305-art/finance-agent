# Phase 7 Real Milvus/BGE Gate Design

## Status and scope

Phase 7 is deliberately limited to the real retrieval gate. It does not replace
the `synthetic_smoke` formal worker, add external finance tools, or claim that the
full research chain is production-ready. The outcome is a reproducible local run
using the pinned `BAAI/bge-large-zh-v1.5` revision and a real Milvus Standalone
collection with BM25+dense hybrid retrieval.

## Options considered

1. **Host BGE + Docker Milvus (selected).** The existing Windows venv loads BGE
   and automatically uses the RTX 4060 when supported; Docker runs Milvus and
   exposes gRPC on localhost only. This is the smallest operational change and
   keeps GPU libraries out of the production-shaped API image.
2. **GPU-enabled RAG container.** More reproducible in theory, but requires NVIDIA
   Container Toolkit, a much larger image and a second image lifecycle. It is not
   justified for this local gate.
3. **CPU-only everything.** Simple but discards available hardware and makes model
   load/quality iteration unnecessarily slow. CPU remains an explicit fallback.

## Runtime and data flow

```text
Chinese evaluation corpus
  -> pinned BGE document embeddings (1024d, normalized)
  -> unique Milvus collection
       text -> Milvus BM25 sparse function
       vector -> dense AUTOINDEX/IP
  -> pinned BGE query embedding with Chinese retrieval instruction
  -> Milvus hybrid_search + RRF
  -> company/scope/profile/index filters
  -> quality, latency and cleanup evidence
```

Milvus binds `127.0.0.1:19530`; it is not remotely exposed. Every verification run
creates a UUID collection and deletes only that exact collection in `finally`.
The persistent application collection is never used by the gate.

## Contracts and failure behavior

- Model name, immutable revision, query instruction, dimension and normalization
  must match `EmbeddingProfile` and its `profile_id`.
- The gate rejects missing dependencies, zero/non-finite/wrong-size embeddings,
  incompatible Milvus schema/functions/indexes, empty results and filter leakage.
- A real BGE gate cannot use `FixedEmbeddings`, hash embeddings or an in-memory
  retriever. The existing fixed-vector Milvus lifecycle test remains a fast SDK
  compatibility test and is reported separately.
- CPU fallback is allowed but must be recorded. Model download/cache failures are
  explicit failures, not BM25-only success for this gate.
- Success records model/device, collection version, timings, Recall/MRR/NDCG,
  filtering assertions and exact cleanup status. No model weights are committed.

## Verification boundary

Acceptance requires: dependency installation; healthy real Milvus; real BGE query
and document embeddings; real hybrid search; company/access/profile/index filters;
stable quality assertions on the small Chinese corpus; exact collection cleanup;
the existing full suite/evals unchanged; and independent subagent review. This
phase does not validate corpus-scale capacity, production financial accuracy,
external tools, LLM report generation or the formal executor.
