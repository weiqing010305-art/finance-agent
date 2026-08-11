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
