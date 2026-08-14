# FinScope baseline evaluations

The harness evaluates the existing HTTP API. It never starts a paid provider by
itself. Start the backend in the desired mode first, then run the evaluator.

Mock smoke run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8780
.\.venv\Scripts\python.exe -m evals.run_eval --limit 1 --output evals/reports/mock-smoke.json
```

Real baseline after configuring `backend/.env`:

```powershell
.\.venv\Scripts\python.exe -m evals.run_eval --output evals/reports/baseline.json --check-urls
```

`--check-urls` performs bounded HTTP requests to evidence URLs. Without it, the
harness only validates URL syntax. Semantic citation support, token usage, and
cost are reported as unavailable until the application exposes reliable data.

Phase 4 离线合同 smoke：

```powershell
.\.venv\Scripts\python.exe -m evals.run_phase4_evals
```

该命令使用明确标识为 `in_memory_test_smoke` 的确定性 hash embedding 和内存检索器，只验证 RRF/过滤/引用/数字溯源合同。真实 BGE/Milvus 指标必须在设置 `MILVUS_TEST_URI` 并启动独立测试 collection 后单独记录，不得与 smoke 指标混用。
Phase 5 memory contract smoke:

```powershell
.\.venv\Scripts\python.exe -m evals.run_phase5_evals
```
