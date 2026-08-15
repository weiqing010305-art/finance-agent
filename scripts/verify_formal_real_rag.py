from __future__ import annotations

import os
import time
from uuid import uuid4

import httpx


def main() -> None:
    email = os.environ["FINSCOPE_SMOKE_EMAIL"]
    password = os.environ["FINSCOPE_SMOKE_PASSWORD"]
    tenant_id = os.environ["FINSCOPE_SMOKE_TENANT"]
    with httpx.Client(
        base_url=os.getenv("FINSCOPE_BASE_URL", "https://localhost:8443"),
        verify=False, trust_env=False, timeout=30,
    ) as client:
        health = client.get("/api/health"); health.raise_for_status()
        if health.json().get("research_executor") != "real_rag_local":
            raise RuntimeError("formal API is not running the real_rag_local profile")
        login = client.post("/api/auth/login", json={
            "email": email, "password": password, "tenant_id": tenant_id,
        })
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post("/api/research", headers={
            **headers, "Idempotency-Key": f"formal-real-rag-{uuid4()}",
        }, json={
            "company": "腾讯", "symbol": "0700.HK", "market": "HK",
            "question": "研究腾讯的现金流、盈利能力与主要风险",
            "depth": "standard", "budget_limit": 20,
        })
        created.raise_for_status()
        body = created.json()
        if body.get("execution_profile") != "real_rag_local":
            raise RuntimeError("created run did not persist the real RAG profile")
        run_id = body["run_id"]
        exercise_pause = os.getenv("FINSCOPE_GATE_PAUSE_RESUME") == "1"
        if exercise_pause:
            paused = client.post(f"/api/research/{run_id}/pause", headers=headers)
            paused.raise_for_status()
        result = None
        for _ in range(180):
            response = client.get(f"/api/research/{run_id}", headers=headers)
            response.raise_for_status(); result = response.json()
            if result["status"] in {"completed", "failed", "paused"}:
                break
            time.sleep(1)
        if exercise_pause:
            if result is None or result["status"] != "paused":
                raise RuntimeError(f"formal real RAG run did not reach paused: {result}")
            resumed = client.post(f"/api/research/{run_id}/resume", headers=headers)
            resumed.raise_for_status()
            for _ in range(180):
                response = client.get(f"/api/research/{run_id}", headers=headers)
                response.raise_for_status(); result = response.json()
                if result["status"] in {"completed", "failed", "paused"}:
                    break
                time.sleep(1)
        if result is None or result["status"] != "completed":
            raise RuntimeError(f"formal real RAG run did not complete: {result}")
        report = result.get("report") or {}
        if report.get("report", {}).get("execution_profile") != "real_rag_local":
            raise RuntimeError("report execution profile mismatch")
        if not report.get("citations") or "local indexed fixture" not in report.get("markdown", ""):
            raise RuntimeError("real RAG report lacks citations or fixture disclosure")
        if "TENANT_A_PRIVATE_MARKER" not in report.get("markdown", ""):
            raise RuntimeError("own-tenant private fixture was not available to the authorized run")
    print(
        f"formal_real_rag_passed run={run_id} citations={len(report['citations'])} "
        f"pause_resume={exercise_pause}"
    )


if __name__ == "__main__":
    main()
