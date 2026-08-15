from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.models import PrincipalContext
from backend.db.durable import DurableConflict, PostgresDurableRepository
from backend.db.artifacts import PostgresResearchArtifacts
from backend.jobs.ledger import JobLedger
from backend.planner import DeterministicPlanner, PlannerError
from backend.schemas import SecurityCandidate


SUPPORTED_EXECUTION_PROFILES = {"synthetic_smoke", "real_rag_local"}


def _job_kind(profile: str) -> str:
    return f"{profile}_research"


def _profile(plan: dict) -> str:
    value = str(plan.get("execution_profile", ""))
    if value not in SUPPORTED_EXECUTION_PROFILES:
        raise DurableConflict("persisted execution profile is unsupported")
    return value


def _profile_available(persisted: str, configured: str) -> bool:
    return persisted == configured or (
        configured == "real_rag_local" and persisted == "synthetic_smoke"
    )


class FormalResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=32)
    market: Literal["CN", "HK", "US", "OTHER"]
    question: str = Field(min_length=5, max_length=2_000)
    depth: Literal["quick", "standard", "deep"] = "standard"
    budget_limit: int = Field(default=20, ge=1, le=10_000)


def build_formal_research_router(
    durable: PostgresDurableRepository, ledger: JobLedger,
    artifacts: PostgresResearchArtifacts, *,
    can_create: Callable, can_read: Callable, sender: Callable[[str], None],
    execution_profile: str = "synthetic_smoke",
) -> APIRouter:
    if execution_profile not in SUPPORTED_EXECUTION_PROFILES:
        raise ValueError("unsupported formal execution profile")
    router = APIRouter(prefix="/api/research", tags=["research"])
    planner = DeterministicPlanner()

    def dispatch(principal: PrincipalContext, job_id: str) -> str:
        if job_id not in ledger.unpublished(principal):
            return "published"
        try:
            sender(job_id)
            ledger.mark_published(principal, job_id)
            return "published"
        except Exception:
            return "outbox_pending"

    @router.post("", status_code=status.HTTP_202_ACCEPTED)
    def create_research(
        payload: FormalResearchRequest,
        idempotency_key: str = Header(min_length=8, max_length=128, alias="Idempotency-Key"),
        principal: PrincipalContext = Depends(can_create),
    ) -> dict:
        entity = SecurityCandidate(
            candidate_id=f"explicit:{payload.market}:{payload.symbol}", company=payload.company,
            symbol=payload.symbol, market=payload.market, confidence=1.0,
            matched_alias=payload.company,
        )
        try:
            if execution_profile == "synthetic_smoke":
                planned = planner.create_plan(
                    question=payload.question, entity=entity, depth=payload.depth,
                    budget_limit=payload.budget_limit,
                )
                plan = {
                    "version": 1, "goal": payload.question,
                    "steps": [{
                        "id": "synthetic_smoke_gate", "kind": "synthesis",
                        "tool_name": "synthetic_smoke", "dependencies": [],
                        "input": {"execution_profile": execution_profile},
                        "success_criteria": ["exercise persistence and verified report gates"],
                        "max_attempts": 1, "estimated_cost": 0,
                    }],
                    "execution_profile": execution_profile,
                    "proposed_external_plan": planned.model_dump(mode="json"),
                    "warning": "proposed external plan is not executed by the synthetic smoke profile",
                }
            else:
                if payload.budget_limit < 2:
                    raise PlannerError("real RAG local plan requires budget_limit >= 2")
                plan = {
                    "version": 1, "goal": payload.question,
                    "entity": entity.model_dump(mode="json"),
                    "steps": [
                        {
                            "id": "retrieve_documents", "kind": "tool",
                            "tool_name": "retrieve_documents", "dependencies": [],
                            "input": {"question": payload.question, "top_k": 5},
                            "success_criteria": ["retrieve at least one authorized traceable chunk"],
                            "max_attempts": 2, "estimated_cost": 2,
                        },
                        {
                            "id": "synthesize_verified_report", "kind": "synthesis",
                            "tool_name": "deterministic_extractive_report",
                            "dependencies": ["retrieve_documents"], "input": {},
                            "success_criteria": ["all claims are extractive and cited"],
                            "max_attempts": 1, "estimated_cost": 0,
                        },
                    ],
                    "execution_profile": execution_profile,
                    "limitations": ["local indexed fixture", "no external tools", "no LLM synthesis"],
                }
            created = durable.create_run(
                principal, company=payload.company, question=payload.question,
                idempotency_key=idempotency_key, plan=plan, owner_id="api-dispatch",
                enqueue_kind=_job_kind(execution_profile),
            )
        except (PlannerError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DurableConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        dispatch_status = dispatch(principal, str(created.job_id))
        return {
            "run_id": created.run_id, "status": "running", "created": created.created,
            "execution_profile": execution_profile, "dispatch_status": dispatch_status,
            "warning": (
                "synthetic smoke execution; not real financial research"
                if execution_profile == "synthetic_smoke"
                else "real local RAG over a labelled indexed fixture; not external financial research"
            ),
        }

    @router.get("/{run_id}")
    def get_research(
        run_id: str, principal: PrincipalContext = Depends(can_read),
    ) -> dict:
        run = durable.get_run(principal, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="research run not found")
        plan = durable.get_latest_plan(principal, run_id)
        if plan is None:
            raise HTTPException(status_code=409, detail="research run has no plan")
        return {
            **run, "execution_profile": _profile(plan),
            "report": artifacts.get_report(principal, run_id),
        }

    @router.post("/{run_id}/pause")
    def pause_research(
        run_id: str, principal: PrincipalContext = Depends(can_create),
    ) -> dict:
        run = durable.get_run(principal, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="research run not found")
        if run["status"] == "pause_requested":
            return run
        try:
            return durable.transition(
                principal, run_id, from_status="running", to_status="pause_requested",
                expected_version=int(run["state_version"]),
            )
        except DurableConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    def resume_research(
        run_id: str, principal: PrincipalContext = Depends(can_create),
    ) -> dict:
        run = durable.get_run(principal, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="research run not found")
        if run["status"] != "paused":
            raise HTTPException(status_code=409, detail="only a paused run can resume")
        try:
            plan = durable.get_latest_plan(principal, run_id)
            if plan is None:
                raise DurableConflict("paused run has no recoverable plan")
            profile = _profile(plan)
            if not _profile_available(profile, execution_profile):
                raise DurableConflict(
                    f"run requires the {profile} executor profile; switch runtime profile before resume"
                )
            job_id = durable.resume_with_job(
                principal, run_id, expected_version=int(run["state_version"]),
                enqueue_kind=_job_kind(profile),
            )
        except DurableConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "run_id": run_id, "status": "resuming", "execution_profile": profile,
            "dispatch_status": dispatch(principal, job_id),
        }

    @router.post("/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_failed_research(
        run_id: str,
        idempotency_key: str = Header(min_length=8, max_length=128, alias="Idempotency-Key"),
        principal: PrincipalContext = Depends(can_create),
    ) -> dict:
        previous = durable.get_run(principal, run_id)
        if previous is None:
            raise HTTPException(status_code=404, detail="research run not found")
        if previous["status"] != "failed":
            raise HTTPException(status_code=409, detail="only a failed run can be retried")
        plan = durable.get_latest_plan(principal, run_id)
        if plan is None:
            raise HTTPException(status_code=409, detail="failed run has no recoverable plan")
        profile = _profile(plan)
        if not _profile_available(profile, execution_profile):
            raise HTTPException(
                status_code=409,
                detail=f"run requires the {profile} executor profile; switch runtime profile before retry",
            )
        try:
            created = durable.create_run(
                principal, company=previous["company"], question=previous["question"],
                idempotency_key=idempotency_key, plan=plan, owner_id="api-retry-dispatch",
                enqueue_kind=_job_kind(profile),
            )
        except DurableConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "run_id": created.run_id, "retried_from": run_id, "status": "running",
            "created": created.created, "execution_profile": profile,
            "dispatch_status": dispatch(principal, str(created.job_id)),
        }

    return router
