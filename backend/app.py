from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from backend.database import Repository, TERMINAL_STATUSES
from backend.agent_graph import RoutingGraph
from backend.context_builder import ContextBuilder
from backend.entity_resolver import EntityResolver
from backend.deepseek_research import (
    DEFAULT_DEEPSEEK_MODEL,
    DeepSeekConfig,
    DeepSeekResearchClient,
)
from backend.environment import load_environment
from backend.durable_runner import DurableRunner, RunConflict
from backend.mock_research import run_mock_research
from backend.planner import DeterministicPlanner, PlannerError
from backend.policy import PolicyGate
from backend.research import run_deepseek_research
from backend.research_executor import ExecutionDenied, ResearchExecutor
from backend.research_graph import ResearchIntakeGraph
from backend.schemas import (
    ConversationRouteRequest,
    ConversationRouteResponse,
    FeedbackCreate,
    EntityConfirmationRequest,
    ResearchCreate,
    ResearchIntakeStartRequest,
    ResearchIntakeStartResponse,
    ResearchIntakeView,
    RouteDecision,
    SecurityCandidate,
    TaskView,
    DeletionJob,
    MemoryDeleteRequest,
    MemoryScope,
    MemoryView,
    PreferenceMemoryWrite,
)
from backend.tool_registry import build_default_registry
from backend.redaction import redact_text
from backend.embeddings import BgeLargeZhEmbeddingProvider, EmbeddingProfile
from backend.milvus_retrieval import MilvusConfig, MilvusHybridRetriever
from backend.research_tools import RetrieveDocumentsTool
from backend.evidence import EvidenceBuilder
from backend.reporting import CitationConstrainedReporter
from backend.schemas import ClaimCandidate
from backend.verifier import ClaimVerifier
from backend.memory import MemoryService, scope_hash
from backend.memory_jobs import MemoryMaintenance
from backend.memory_consolidation import ReportMemoryConsolidator


DEFAULT_DATABASE = Path(__file__).parent / "data" / "finscope.db"


def create_app(
    database_path: str | Path | None = None,
    mock_delay: float | None = None,
    research_mode: str | None = None,
    load_env_file: bool = True,
) -> FastAPI:
    if load_env_file:
        load_environment(Path(__file__).parent / ".env")
    repository = Repository(database_path or os.getenv("FINSCOPE_DB", DEFAULT_DATABASE))
    memory_service = MemoryService(repository)
    memory_maintenance = MemoryMaintenance(repository)
    memory_consolidator = ReportMemoryConsolidator(repository)
    runner = DurableRunner(repository)
    routing_graph = RoutingGraph(ContextBuilder(repository))
    intake_graph = ResearchIntakeGraph(repository, EntityResolver())
    planner = DeterministicPlanner()
    embedding_profile = EmbeddingProfile(
        revision=os.getenv("EMBEDDING_MODEL_REVISION", EmbeddingProfile().revision)
    )
    embedding_provider = BgeLargeZhEmbeddingProvider(
        profile=embedding_profile, device=os.getenv("EMBEDDING_DEVICE", "cpu")
    )
    milvus_retriever = MilvusHybridRetriever(
        MilvusConfig(
            uri=os.getenv("MILVUS_URI", "http://127.0.0.1:19530"),
            token=os.getenv("MILVUS_TOKEN") or None,
            collection=os.getenv("MILVUS_COLLECTION", "finance_agent_chunks_v1"),
        ),
        embedding_provider,
    )
    retrieval_tool = RetrieveDocumentsTool(
        repository, milvus_retriever, embedding_profile=embedding_profile,
        index_version=os.getenv("MILVUS_INDEX_VERSION", "finance-chunks-v1"),
    )
    # Explicit mock_delay is the deterministic offline test/demo profile. It keeps
    # the adapter visibly unconfigured instead of pretending an in-memory index is Milvus.
    phase3_registry = build_default_registry(
        retrieval_handler=None if mock_delay is not None else retrieval_tool
    )
    phase3_executor = ResearchExecutor(
        runner, phase3_registry, PolicyGate(repository, phase3_registry), planner
    )
    owner_id = f"api-{uuid4()}"
    lease_tokens: dict[str, str] = {}
    delay = mock_delay if mock_delay is not None else float(os.getenv("FINSCOPE_MOCK_DELAY", "0.65"))
    selected_mode = research_mode or ("mock" if mock_delay is not None else None)
    mode = (selected_mode or os.getenv("FINSCOPE_RESEARCH_MODE", "mock")).strip().lower()
    if mode not in {"mock", "deepseek"}:
        raise ValueError("FINSCOPE_RESEARCH_MODE must be 'mock' or 'deepseek'")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_client = DeepSeekResearchClient(
        DeepSeekConfig(
            api_key=deepseek_key,
            model=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL,
        )
    )
    running_tasks: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository.initialize()
        app.state.repository = repository
        app.state.runner = runner
        app.state.routing_graph = routing_graph
        app.state.intake_graph = intake_graph
        app.state.phase3_executor = phase3_executor
        recovered = runner.reconcile_expired_runs(owner_id=owner_id)
        for item in recovered:
            task_id = item.run["id"]
            lease_tokens[task_id] = item.lease_token
            spawn_worker(task_id)
        yield
        for task in tuple(running_tasks.values()):
            task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks.values(), return_exceptions=True)
        repository.expire_owner_leases(owner_id)

    app = FastAPI(
        title="FinScope Research API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "null",  # Local file:// MVP opened directly from Explorer.
            "http://127.0.0.1:8770",
            "http://localhost:8770",
            "http://127.0.0.1:8771",
            "http://localhost:8771",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def spawn(task_id: str, coroutine) -> None:
        existing = running_tasks.get(task_id)
        if existing is not None and not existing.done():
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        running_tasks[task_id] = task

        def remove_finished(finished: asyncio.Task) -> None:
            if running_tasks.get(task_id) is finished:
                running_tasks.pop(task_id, None)

        task.add_done_callback(remove_finished)

    def spawn_worker(task_id: str) -> None:
        token_provider = lambda active_id=task_id: lease_tokens[active_id]
        if repository.get_research_intake_by_run(task_id) is not None:
            spawn(task_id, run_phase3_research(task_id, token_provider))
            return
        if mode == "deepseek":
            spawn(task_id, run_deepseek_research(runner, task_id, token_provider, deepseek_client))
        else:
            spawn(task_id, run_mock_research(runner, task_id, token_provider, delay))

    async def run_phase3_research(task_id: str, token_provider) -> None:
        try:
            intake = repository.get_research_intake_by_run(task_id)
            if intake is None:
                raise RuntimeError("Phase 3 run is missing its intake")
            route_row = repository.get_route_request(intake["route_request_id"])
            if route_row is None:
                raise RuntimeError("Phase 3 run is missing its route")
            decision = RouteDecision.model_validate(route_row["decision"])
            while True:
                task = repository.get_task(task_id)
                if task is None or task["status"] in TERMINAL_STATUSES:
                    return
                if task["status"] == "pause_requested":
                    runner.acknowledge_pause(task_id, lease_token=token_provider())
                    return
                if task["status"] != "running":
                    return
                batch = await phase3_executor.execute_ready_batch(
                    task_id,
                    lease_token=token_provider(),
                    route=decision,
                    entity_confirmed=intake["resolved_entity"] is not None,
                    budget_limit=int(intake["budget_limit"]),
                )
                if not batch.executed_step_ids:
                    snapshot = repository.get_runtime_snapshot(task_id)
                    frontier = snapshot["checkpoint"]["frontier"]
                    if not frontier.get("ready_step_ids") and not frontier.get("blocked_step_ids"):
                        repository.append_runtime_event(
                            task_id,
                            kind="research.execution_completed",
                            step="awaiting_report",
                            progress=95,
                            message="研究步骤已执行完毕，等待证据验证与报告阶段",
                            payload={"phase": 3, "plan_version": frontier.get("plan_version")},
                            lease_token=token_provider(),
                        )
                        await run_phase4_report(task_id, token_provider)
                    return
        except Exception as exc:
            task = repository.get_task(task_id)
            if task is not None and task["status"] in {"running", "pause_requested", "resuming"}:
                runner.fail_run(
                    task_id, lease_token=token_provider(),
                    error=redact_text(f"Phase 3 execution failed: {type(exc).__name__}"),
                )

    async def run_phase4_report(task_id: str, token_provider) -> None:
        task = repository.get_task(task_id)
        if task is None:
            return
        if task["status"] == "pause_requested":
            runner.acknowledge_pause(task_id, lease_token=token_provider())
            return
        if task["status"] != "running":
            return
        snapshot = repository.get_runtime_snapshot(task_id)
        evidence_hits: list[dict] = []
        for step in snapshot["steps"]:
            if step["status"] != "succeeded" or not step.get("output_json"):
                continue
            try:
                output = json.loads(step["output_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            evidence_by_source = {
                str(item.get("source_id")): item
                for item in output.get("evidence", []) if isinstance(item, dict)
            }
            for item in output.get("data", []) if isinstance(output.get("data"), list) else []:
                if not isinstance(item, dict) or not (item.get("text") or item.get("snippet")):
                    continue
                metadata = evidence_by_source.get(str(item.get("source_id")), {})
                evidence_hits.append({
                    **metadata, **item,
                    "text": item.get("text") or item.get("snippet"),
                    "source_uri": metadata.get("url") or item.get("url"),
                    "document_version_id": item.get("source_id"),
                    # Tool-provided publisher labels are untrusted. A versioned
                    # authority registry is required before assigning tier >= 2.
                    "authority_tier": 1,
                })
        builder = EvidenceBuilder()
        evidence = builder.build_retrieval_items(task_id, evidence_hits)
        claims = ClaimVerifier().verify(
            [
                ClaimCandidate(
                    id=f"claim_{item.id.removeprefix('ev_')}", run_id=task_id,
                    text=item.excerpt[:1000], evidence_ids=[item.id],
                    period=item.period,
                    unit="source_text" if any(char.isdigit() for char in item.excerpt) else None,
                )
                for item in evidence
            ],
            evidence,
            allowed_access_scopes={"public"},
        )
        runner.persist_verified_evidence(
            task_id, lease_token=token_provider(), evidence=evidence, claims=claims,
        )
        current = repository.get_task(task_id)
        if current is not None and current["status"] == "pause_requested":
            runner.acknowledge_pause(task_id, lease_token=token_provider())
            return
        if current is None or current["status"] != "running":
            return
        reporter = CitationConstrainedReporter()
        draft = reporter.build_deterministic(
            company=task["company"], question=task["question"],
            claims=claims, evidence=evidence,
        )
        markdown, report_json, citations = reporter.render(draft, claims, evidence)
        generation_key = "phase4-deterministic-v1"
        runner.persist_report_snapshot(
            task_id, lease_token=token_provider(), generation_key=generation_key,
            model="deterministic", schema_version=1,
            snapshot={
                "markdown": f"# {task['company']}研究报告\n\n{draft.summary}\n",
                "report": {"company": task["company"], "summary": draft.summary},
                "complete": False,
            },
        )
        current = repository.get_task(task_id)
        if current is not None and current["status"] == "pause_requested":
            runner.acknowledge_pause(task_id, lease_token=token_provider())
            return
        if current is None or current["status"] != "running":
            return
        runner.persist_report_snapshot(
            task_id, lease_token=token_provider(), generation_key=generation_key,
            model="deterministic", schema_version=1,
            snapshot={"markdown": markdown, "report": report_json, "complete": True},
        )
        current = repository.get_task(task_id)
        if current is not None and current["status"] == "pause_requested":
            runner.acknowledge_pause(task_id, lease_token=token_provider())
            return
        if current is None or current["status"] != "running":
            return
        runner.complete_verified_report(
            task_id, lease_token=token_provider(), generation_key=generation_key,
            markdown=markdown, report_json=report_json, citations=citations,
            degraded=draft.degraded,
        )
        memory_consolidator.consolidate(task_id)

    def start_phase3_run(intake: dict) -> dict:
        if intake["run_id"]:
            task = repository.get_task(intake["run_id"])
            if task is None:
                raise HTTPException(status_code=409, detail="intake 绑定的研究任务不存在")
            return repository.get_research_intake(intake["id"])
        if intake["status"] != "ready" or not intake["resolved_entity"]:
            return intake
        entity = SecurityCandidate.model_validate(intake["resolved_entity"])
        route_row = repository.get_route_request(intake["route_request_id"])
        if route_row is None:
            raise HTTPException(status_code=409, detail="研究路由不存在")
        try:
            plan = planner.create_plan(
                question=intake["message"], entity=entity, depth=intake["depth"],
                budget_limit=int(intake["budget_limit"]), version=1,
            )
            target_case_id = route_row["case_id"]
            if target_case_id is not None:
                current_case = repository.get_case(target_case_id)
                if current_case is not None and (
                    (current_case.get("symbol") and current_case["symbol"] != entity.symbol)
                    or current_case["company"] != entity.company
                ):
                    target_case_id = None
            created = runner.create_run(
                ResearchCreate(
                    company=entity.company,
                    symbol=entity.symbol,
                    market=entity.market,
                    question=intake["message"],
                    depth=intake["depth"],
                ),
                owner_id=owner_id,
                idempotency_key=f"intake:{intake['id']}",
                case_id=target_case_id,
                intake_id=intake["id"],
                initial_plan=plan.model_dump(),
            )
        except (ValueError, RunConflict, PlannerError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        lease_tokens[created.run["id"]] = created.lease_token
        spawn_worker(created.run["id"])
        linked = repository.get_research_intake(intake["id"])
        assert linked is not None
        return linked

    def require_task(task_id: str) -> dict:
        task = repository.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="研究任务不存在")
        return task

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        response = {"status": "ok", "mode": mode}
        if mode == "deepseek":
            response["provider"] = "deepseek"
            response["model"] = deepseek_client.config.model
            response["configured"] = "true" if deepseek_key else "false"
        return response

    @app.post("/api/conversations/route", response_model=ConversationRouteResponse)
    async def route_conversation(payload: ConversationRouteRequest) -> dict:
        request_id = payload.request_id or str(uuid4())
        case_id = payload.case_id
        if case_id is not None and repository.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="研究 case 不存在")
        existing_route = repository.get_route_request(request_id)
        if existing_route is not None and (
            existing_route["case_id"] != case_id
            or existing_route["message"] != payload.message
        ):
            raise HTTPException(
                status_code=409,
                detail="request id was already used with a different route request",
            )
        if case_id is not None:
            try:
                repository.append_conversation_turn(
                    case_id,
                    turn_id=f"route:{request_id}:user",
                    role="user",
                    content=payload.message,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if existing_route is None:
            try:
                routed = app.state.routing_graph.route(payload.message, case_id=case_id)
                decision = RouteDecision.model_validate(routed["decision"])
                response_text = str(routed["response"])
                trace = list(routed.get("trace", []))
            except Exception:
                decision = RouteDecision(
                    intent="CLARIFICATION",
                    confidence=0.0,
                    case_id=case_id,
                    requires_planner=False,
                    external_research_allowed=False,
                    response_policy="ask_clarification",
                    reason_codes=["ROUTING_FAILURE"],
                )
                response_text = "路由暂时不可用，请重新说明你想研究的公司或问题。"
                trace = ["routing_failure"]
            try:
                existing_route = repository.save_route_request_result(
                    request_id,
                    case_id=case_id,
                    message=payload.message,
                    decision=decision.model_dump(),
                    response=response_text,
                    trace=trace,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        decision = RouteDecision.model_validate(existing_route["decision"])
        response_text = str(existing_route["response"])
        trace = list(existing_route["trace"])
        if case_id is not None:
            try:
                repository.append_conversation_turn(
                    case_id,
                    turn_id=f"route:{request_id}:assistant",
                    role="assistant",
                    content=response_text,
                    intent=decision.intent,
                    reason_codes=decision.reason_codes,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "request_id": request_id,
            "case_id": case_id,
            "decision": decision,
            "response": response_text,
            "trace": trace,
        }

    @app.post("/api/research/intakes", response_model=ResearchIntakeStartResponse)
    async def start_research_intake(payload: ResearchIntakeStartRequest) -> dict:
        try:
            result = app.state.intake_graph.start(payload)
            intake = start_phase3_run(result.intake)
            return {"intake": intake, "trace": result.trace}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="研究路由不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/research/intakes/{intake_id}", response_model=ResearchIntakeView)
    async def get_research_intake(intake_id: str) -> dict:
        intake = repository.get_research_intake(intake_id)
        if intake is None:
            raise HTTPException(status_code=404, detail="研究 intake 不存在")
        return intake

    @app.post("/api/research/intakes/{intake_id}/confirm", response_model=ResearchIntakeView)
    async def confirm_research_entity(
        intake_id: str, payload: EntityConfirmationRequest
    ) -> dict:
        try:
            intake = repository.resolve_entity_confirmation(
                intake_id, candidate_id=payload.candidate_id
            )
            return start_phase3_run(intake)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="研究 intake 不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/research", response_model=TaskView, status_code=status.HTTP_202_ACCEPTED)
    async def create_research(
        payload: ResearchCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        if payload.agent != "financial":
            raise HTTPException(status_code=409, detail="前端 MVP 目前只启用财报分析 Agent")
        if mode == "deepseek" and not deepseek_key:
            raise HTTPException(
                status_code=503,
                detail="缺少 DEEPSEEK_API_KEY，无法启动 DeepSeek 联网研究",
            )
        try:
            created = runner.create_run(
                payload,
                owner_id=owner_id,
                idempotency_key=idempotency_key or str(uuid4()),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        task = created.run
        if not created.created:
            return task
        lease_tokens[task["id"]] = created.lease_token
        spawn_worker(task["id"])
        return task

    @app.get("/api/research/{task_id}", response_model=TaskView)
    async def get_research(task_id: str) -> dict:
        return require_task(task_id)

    @app.post("/api/research/{task_id}/evidence/enrich", response_model=TaskView)
    async def enrich_research_evidence(task_id: str) -> dict:
        task = require_task(task_id)
        if task["status"] != "completed":
            raise HTTPException(
                status_code=409,
                detail="只有已完成报告可以补充证据元数据",
            )
        enriched = await deepseek_client.enrich_evidence(task["evidence"])
        try:
            return repository.enrich_completed_evidence(task_id, enriched)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/research/{task_id}/events")
    async def stream_research(
        request: Request,
        task_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        require_task(task_id)
        try:
            cursor = max(after, int(last_event_id or 0))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc

        async def event_stream() -> AsyncIterator[str]:
            nonlocal cursor
            idle_ticks = 0
            while True:
                if await request.is_disconnected():
                    return
                events = repository.list_events(task_id, cursor)
                if events:
                    idle_ticks = 0
                    for event in events:
                        cursor = event["id"]
                        payload = json.dumps(event, ensure_ascii=False)
                        yield f"id: {cursor}\nevent: progress\ndata: {payload}\n\n"
                else:
                    idle_ticks += 1

                task = repository.get_task(task_id)
                if task is None or (task["status"] in TERMINAL_STATUSES and not events):
                    return
                if idle_ticks >= 40:
                    idle_ticks = 0
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/research/{task_id}/pause", response_model=TaskView)
    async def pause_research(task_id: str) -> dict:
        task = require_task(task_id)
        if task["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="已结束的任务不能暂停")
        try:
            return runner.request_pause(task_id)
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/research/{task_id}/resume", response_model=TaskView)
    async def resume_research(task_id: str) -> dict:
        task = require_task(task_id)
        if task["status"] != "paused":
            raise HTTPException(status_code=409, detail="只有暂停中的任务可以继续")
        try:
            resuming = runner.request_resume(task_id, owner_id=owner_id)
            token = resuming.pop("lease_token")
            lease_tokens[task_id] = token
            running = runner.finish_resume(task_id, lease_token=token)
            spawn_worker(task_id)
            return running
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/research/{task_id}/cancel", response_model=TaskView)
    async def cancel_research(task_id: str) -> dict:
        task = require_task(task_id)
        if task["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="研究任务已经结束")
        try:
            # Compatibility endpoint: permanent cancellation is not part of the six-state contract.
            return runner.request_pause(task_id)
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/research/{task_id}/feedback", response_model=TaskView)
    async def submit_feedback(task_id: str, payload: FeedbackCreate) -> dict:
        require_task(task_id)
        return repository.add_feedback(task_id, payload.message)

    @app.get("/api/cases")
    async def list_cases() -> list[dict]:
        return repository.list_cases()

    @app.get("/api/memory", response_model=list[MemoryView])
    async def list_my_memory():
        principal_scope = MemoryScope(
            scope_kind="user", tenant_id="local", user_id="default"
        )
        return repository.query_active_memories(
            scope_hashes=[scope_hash(principal_scope)], limit=100
        )

    @app.post("/api/memory/preferences", response_model=MemoryView)
    async def write_preference(payload: PreferenceMemoryWrite):
        from backend.schemas import MemoryCandidate
        try:
            return memory_service.remember(MemoryCandidate(
                memory_type="user_preference", memory_key=payload.memory_key,
                scope=MemoryScope(
                    scope_kind="user", tenant_id="local", user_id="default"
                ),
                content=payload.value, content_text=payload.text,
                idempotency_key=payload.idempotency_key, confidence=1,
                explicit_user_confirmation=True,
            ))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=redact_text(str(exc))) from exc

    @app.delete("/api/memory/{memory_id}", response_model=DeletionJob)
    async def delete_my_memory(memory_id: str, payload: MemoryDeleteRequest):
        try:
            job = repository.tombstone_memory_atomic(
                memory_id, tenant_id="local", user_id="default",
                idempotency_key=payload.idempotency_key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="memory not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=redact_text(str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=redact_text(str(exc))) from exc
        return job

    @app.post("/api/memory/deletions/{job_id}/process", response_model=DeletionJob)
    async def process_memory_deletion(job_id: str):
        if repository.get_memory_deletion_job_for_principal(
            job_id, tenant_id="local", user_id="default"
        ) is None:
            raise HTTPException(status_code=404, detail="deletion job not found")
        try:
            return memory_maintenance.delete(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="deletion job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=redact_text(str(exc))) from exc

    @app.get("/api/memory/deletions/{job_id}", response_model=DeletionJob)
    async def get_memory_deletion(job_id: str):
        job = repository.get_memory_deletion_job_for_principal(
            job_id, tenant_id="local", user_id="default"
        )
        if job is None:
            raise HTTPException(status_code=404, detail="deletion job not found")
        return job

    @app.post("/api/memory/clear", response_model=list[DeletionJob])
    async def clear_my_private_memory(payload: MemoryDeleteRequest):
        return repository.tombstone_all_private_memories_atomic(
            tenant_id="local", user_id="default",
            idempotency_prefix=payload.idempotency_key,
        )

    return app


app = create_app()
