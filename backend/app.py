from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from backend.database import Repository, TERMINAL_STATUSES
from backend.deepseek_research import (
    DEFAULT_DEEPSEEK_MODEL,
    DeepSeekConfig,
    DeepSeekResearchClient,
)
from backend.environment import load_environment
from backend.mock_research import run_mock_research
from backend.openrouter_research import DEFAULT_MODEL, OpenRouterConfig, OpenRouterResearchClient
from backend.research import run_deepseek_research, run_openrouter_research
from backend.schemas import FeedbackCreate, ResearchCreate, TaskView


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
    delay = mock_delay if mock_delay is not None else float(os.getenv("FINSCOPE_MOCK_DELAY", "0.65"))
    selected_mode = research_mode or ("mock" if mock_delay is not None else None)
    mode = (selected_mode or os.getenv("FINSCOPE_RESEARCH_MODE", "mock")).strip().lower()
    if mode not in {"mock", "openrouter", "deepseek"}:
        raise ValueError("FINSCOPE_RESEARCH_MODE must be 'mock', 'openrouter' or 'deepseek'")
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    openrouter_client = OpenRouterResearchClient(
        OpenRouterConfig(
            api_key=openrouter_key,
            model=os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            search_engine=os.getenv("OPENROUTER_SEARCH_ENGINE", "parallel").strip() or "parallel",
            max_results=max(1, min(int(os.getenv("OPENROUTER_MAX_RESULTS", "10")), 20)),
        )
    )
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_client = DeepSeekResearchClient(
        DeepSeekConfig(
            api_key=deepseek_key,
            model=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL,
        )
    )
    running_tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository.initialize()
        app.state.repository = repository
        yield
        for task in tuple(running_tasks):
            task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

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

    def spawn(coroutine) -> None:
        task = asyncio.create_task(coroutine)
        running_tasks.add(task)
        task.add_done_callback(running_tasks.discard)

    def require_task(task_id: str) -> dict:
        task = repository.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="研究任务不存在")
        return task

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        response = {"status": "ok", "mode": mode}
        if mode == "openrouter":
            response["provider"] = "openrouter"
            response["model"] = openrouter_client.config.model
            response["configured"] = "true" if openrouter_key else "false"
        elif mode == "deepseek":
            response["provider"] = "deepseek"
            response["model"] = deepseek_client.config.model
            response["configured"] = "true" if deepseek_key else "false"
        return response

    @app.post("/api/research", response_model=TaskView, status_code=status.HTTP_202_ACCEPTED)
    async def create_research(payload: ResearchCreate) -> dict:
        if payload.agent != "financial":
            raise HTTPException(status_code=409, detail="前端 MVP 目前只启用财报分析 Agent")
        if mode == "openrouter" and not openrouter_key:
            raise HTTPException(
                status_code=503,
                detail="缺少 OPENROUTER_API_KEY，无法启动真实研究",
            )
        if mode == "deepseek" and not deepseek_key:
            raise HTTPException(
                status_code=503,
                detail="缺少 DEEPSEEK_API_KEY，无法启动 DeepSeek 联网研究",
            )
        task = repository.create_task(payload)
        if mode == "openrouter":
            spawn(run_openrouter_research(repository, task["id"], openrouter_client))
        elif mode == "deepseek":
            spawn(run_deepseek_research(repository, task["id"], deepseek_client))
        else:
            spawn(run_mock_research(repository, task["id"], delay))
        return task

    @app.get("/api/research/{task_id}", response_model=TaskView)
    async def get_research(task_id: str) -> dict:
        return require_task(task_id)

    @app.post("/api/research/{task_id}/evidence/enrich", response_model=TaskView)
    async def enrich_research_evidence(task_id: str) -> dict:
        task = require_task(task_id)
        enriched = await deepseek_client.enrich_evidence(task["evidence"])
        repository.replace_evidence(task_id, enriched)
        return require_task(task_id)

    @app.get("/api/research/{task_id}/events")
    async def stream_research(
        request: Request,
        task_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        require_task(task_id)
        cursor = max(after, int(last_event_id or 0))

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
        return repository.update_task(
            task_id,
            status="paused",
            step=task["current_step"],
            progress=task["progress"],
            message="研究已暂停，当前结果已保留",
            kind="task.paused",
        )

    @app.post("/api/research/{task_id}/resume", response_model=TaskView)
    async def resume_research(task_id: str) -> dict:
        task = require_task(task_id)
        if task["status"] != "paused":
            raise HTTPException(status_code=409, detail="只有暂停中的任务可以继续")
        return repository.update_task(
            task_id,
            status="running",
            step=task["current_step"],
            progress=task["progress"],
            message="研究已继续",
            kind="task.resumed",
        )

    @app.post("/api/research/{task_id}/cancel", response_model=TaskView)
    async def cancel_research(task_id: str) -> dict:
        task = require_task(task_id)
        if task["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="研究任务已经结束")
        return repository.update_task(
            task_id,
            status="cancelled",
            step="cancelled",
            progress=task["progress"],
            message="研究已停止，当前结果已保留",
            kind="task.cancelled",
        )

    @app.post("/api/research/{task_id}/feedback", response_model=TaskView)
    async def submit_feedback(task_id: str, payload: FeedbackCreate) -> dict:
        require_task(task_id)
        return repository.add_feedback(task_id, payload.message)

    @app.get("/api/cases")
    async def list_cases() -> list[dict]:
        return repository.list_cases()

    return app


app = create_app()
