# Phase 7 Real Milvus/BGE Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reproducibly execute and measure real `bge-large-zh-v1.5` embeddings against real Milvus BM25+dense hybrid retrieval on the local machine.

**Architecture:** Keep BGE in the host Python venv with automatic CUDA/CPU selection and run Milvus in Docker with a localhost-only gRPC port. A dedicated gate script creates a unique collection, evaluates a fixed Chinese corpus, writes a machine-readable result, and removes only its own collection.

**Tech Stack:** Python 3.12, sentence-transformers, PyTorch, PyMilvus 2.6, Milvus Standalone 2.6, Docker Compose, pytest.

---

### Task 1: Make the local Milvus profile operable and health-checked

**Files:**
- Modify: `compose.yaml`
- Modify: `scripts/milvus/README.md`
- Test: `tests/test_local_operations_contract.py`

1. Add the minimum standalone dependencies/config required by Milvus 2.6.
2. Bind Milvus gRPC only to `127.0.0.1:19530` and add a healthcheck.
3. Run all Compose profile config checks.
4. Start the RAG profile and prove the real server is healthy.

### Task 2: Pin and validate the real BGE runtime

**Files:**
- Modify: `requirements-rag.txt`
- Modify: `backend/embeddings.py`
- Test: `tests/test_embeddings.py`

1. Add failing tests for runtime metadata and explicit device reporting.
2. Keep model/revision/dimension/query instruction immutable.
3. Install the RAG dependencies into `.venv` and load the real model.
4. Prove document/query vectors are finite, normalized and 1024-dimensional.

### Task 3: Build the destructive-safe real gate

**Files:**
- Create: `scripts/verify_real_rag.py`
- Modify: `tests/integration/test_milvus_hybrid.py`
- Create: `evals/real-rag-cases.json`

1. Write a small Chinese corpus with relevant, distractor and cross-company rows.
2. Create a UUID collection through `MilvusHybridRetriever`.
3. Embed documents with real BGE, upsert, flush and hybrid search.
4. Assert top-k relevance and company/access/profile/index filtering.
5. Record device, timings, quality metrics and exact cleanup in JSON.
6. Add environment-gated pytest coverage that invokes the real model path.

### Task 4: Run gates and publish truthful evidence

**Files:**
- Create: `docs/reviews/phase-7-verification.md`
- Modify: `docs/architecture/durable-research-agent.md`
- Modify: `README.md`

1. Run the real gate twice to prove cached-model reproducibility.
2. Run the fixed-vector Milvus integration test separately.
3. Run the full pytest suite, Phase 3/4/5 evals, compileall and diff-check.
4. Document exact model revision, device, Milvus version, metrics and limitations.
5. Request independent subagent review, fix every P0/P1, and stop for user approval.
