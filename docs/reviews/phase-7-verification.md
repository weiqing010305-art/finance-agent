# Phase 7 Verification — Real Milvus/BGE Gate

Status: **PASS — locally executed and independently reviewed (`P0=0`, `P1=0`).**

## Scope

- Real Milvus Standalone `2.6.2`, with dedicated etcd `3.5.18` and MinIO,
  persistent volumes, health dependencies and localhost-only ports.
- PyMilvus `2.6.17` and sentence-transformers `5.7.0` pinned in
  `requirements-rag.txt`.
- Real `BAAI/bge-large-zh-v1.5` revision
  `79e7739b6ab944e86d6171e44d24c997fc1e0116`, profile
  `emb_8f7a7aacc9e57bc8ed1965d3`, 1024 dimensions and normalized vectors.
- A destructive-safe CLI gate using a UUID collection, Milvus BM25+dense RRF,
  structured filters, machine-readable metrics and exact collection cleanup.

## Real execution evidence (2026-08-14)

- All three RAG containers healthy; Milvus reports server version `2.6.2`.
- BGE initial load produced two finite normalized 1024-dimensional vectors.
  The host environment installed `torch 2.13.0+cpu`, so the recorded runtime
  device is CPU (`cuda_available=false`) despite an available NVIDIA GPU.
- Gate run 1: document embedding `6.018s`, upsert/flush `3.594s`, four query
  latencies `137.572–139.901ms`, total before cleanup `10.414s`.
- Gate run 2: document embedding `5.795s`, upsert/flush `3.792s`, four query
  latencies `127.242–140.800ms`, total before cleanup `10.362s`.
- Both runs: Recall@3 `1.0`, MRR@3 `1.0`, NDCG@3 `1.0`; relevant result was
  rank 1 in all four queries.
- Both runs rejected private-scope leakage and incompatible company/profile/index
  rows; both UUID collections were absent after cleanup.
- Environment-enabled integration command produced `2 passed` (fixed-vector
  lifecycle plus real-BGE quality gate).

## Regression gates

- Default full suite: `403 passed, 2 skipped, 2 warnings` in `22.11s`.
  The two skips are the explicit real-Milvus and real-BGE environment gates;
  they were executed separately above. Warnings are a dependency deprecation
  and a local pytest-cache permission warning.
- Phase 3, Phase 4 offline and Phase 5 evals: pass with their previously declared
  smoke modes and no failures.
- `compileall`: pass; `pip check`: no broken requirements; Compose config: pass;
  `git diff --check`: no whitespace error (line-ending warnings only).

## Commands

```powershell
docker compose --profile rag up -d milvus-etcd milvus-minio milvus
$env:MILVUS_TEST_URI = "http://127.0.0.1:19530"
$env:RUN_REAL_BGE_TEST = "1"
.\.venv\Scripts\python.exe -m scripts.verify_real_rag
.\.venv\Scripts\python.exe -m pytest tests/integration/test_milvus_hybrid.py -q
```

## Explicit limitations

- The eight-row corpus is synthetic and tests wiring/filters, not production
  financial retrieval quality. No corpus-scale Recall, concurrency, capacity or
  soak test has been run.
- The response currently labels both native Milvus hybrid fusion and the
  client-side fallback as `mode=hybrid`. Independent review disabled the
  fallback and proved the native route passes; a future trace field should make
  `native_milvus` versus `client_fallback` observable per query.
- CPU execution is accepted for correctness. CUDA acceleration has not been
  installed or benchmarked.
- The formal worker remains `synthetic_smoke`; Phase 7 does not claim external
  research tools, real report generation or end-to-end production RAG.
- Model assets live in the local Hugging Face cache and are not committed.

## Independent review

Passed on 2026-08-14 with `P0=0`, `P1=0`, `P2=2`. The reviewer independently
validated distinct real 1024-dimensional unit vectors, the pinned cached model
commit, secret separation, localhost-only bindings, persistent volumes, native
Milvus hybrid execution with fallback forcibly disabled, filter non-leakage and
exception-path collection cleanup. Its full regression result was
`403 passed, 2 skipped, 2 warnings`; the environment-enabled focused run was
`18 passed`.
