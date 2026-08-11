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
from backend.deepseek_research import (
    DEFAULT_DEEPSEEK_MODEL,
    DeepSeekConfig,
    DeepSeekResearchClient,
)
from backend.environment import load_environment
from backend.durable_runner import DurableRunner, RunConflict
from backend.mock_research import run_mock_research
from backend.research import run_deepseek_research
from backend.schemas import (
    ConversationRouteRequest,
    ConversationRouteResponse,
    FeedbackCreate,
    ResearchCreate,
    RouteDecision,
    TaskView,
)


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
    runner = DurableRunner(repository)
    routing_graph = RoutingGraph(ContextBuilder(repository))
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
        if mode == "deepseek":
            spawn(task_id, run_deepseek_research(runner, task_id, token_provider, deepseek_client))
        else:
            spawn(task_id, run_mock_research(runner, task_id, token_provider, delay))

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

    return app


app = create_app()
