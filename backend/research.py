from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import urldefrag, urlparse

from backend.database import Repository, TERMINAL_STATUSES
from backend.deepseek_research import DeepSeekResearchClient
from backend.durable_runner import DurableRunner
from backend.redaction import redact_text, redact_url


def _safe_url(value: str) -> str:
    return redact_url(value)


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return redact_text(message)[:500]


async def _continue_when_running(
    runner: DurableRunner,
    task_id: str,
    lease_token_provider: Callable[[], str],
) -> bool:
    while True:
        task = runner.repository.get_task(task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return False
        if task["status"] == "pause_requested":
            runner.acknowledge_pause(task_id, lease_token=lease_token_provider())
        elif task["status"] == "running":
            return True
        await asyncio.sleep(0.1)


def _deepseek_trace(event: dict) -> tuple[str, int, str, dict] | None:
    event_type = str(event.get("type") or "")
    if event_type == "finscope.report_recovery":
        return "writing", 88, "未收到报告正文，正在停止搜索并重新整理报告", {
            "provider_event": event_type,
        }
    if event_type == "response.web_search_call.searching":
        return "searching", 28, "DeepSeek 正在联网搜索", {"provider_event": event_type}
    if event_type == "response.web_search_call.completed":
        return "reading", 58, "网页搜索完成，正在阅读与整理来源", {"provider_event": event_type}
    if event_type == "response.reasoning_text.delta":
        return "reasoning", 68, "正在分析已检索的信息", {"provider_event": event_type}
    if event_type == "response.output_text.delta":
        return "writing", 84, "正在生成研究报告", {"provider_event": event_type}
    if event_type != "response.output_item.done":
        return None

    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    if item.get("type") != "web_search_call":
        return None
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    action_type = str(action.get("type") or "")
    if action_type == "search":
        query = str(action.get("query") or "").strip()
        return "searching", 42, f"搜索：{query}" if query else "正在检索相关网页", {
            "provider_event": event_type,
            "action": "search",
            "query": query,
        }
    if action_type in {"open_page", "open"}:
        url = str(action.get("url") or "").strip()
        return "reading", 56, f"浏览页面：{url}" if url else "正在浏览检索结果", {
            "provider_event": event_type,
            "action": "open_page",
            "url": url,
        }
    if action_type in {"find", "find_in_page"}:
        pattern = str(action.get("pattern") or action.get("text") or "").strip()
        return "reading", 62, f"在页面内查找：{pattern}" if pattern else "正在定位页面中的相关内容", {
            "provider_event": event_type,
            "action": "find_in_page",
            "pattern": pattern,
        }
    return None


async def run_deepseek_research(
    runner: DurableRunner | Repository,
    task_id: str,
    lease_token_provider: Callable[[], str] | DeepSeekResearchClient,
    client: DeepSeekResearchClient | None = None,
) -> None:
    if isinstance(runner, Repository):
        repository = runner
        runner = DurableRunner(repository)
        snapshot = repository.get_runtime_snapshot(task_id)
        lease = snapshot["lease"]
        if lease is None:
            raise RuntimeError("legacy research call requires an active run lease")
        token = str(lease["lease_token"])
        client = lease_token_provider  # type: ignore[assignment]
        lease_token_provider = lambda: token
    if client is None or not callable(lease_token_provider):
        raise TypeError("client and lease token provider are required")
    repository = runner.repository
    emitted: set[tuple[str, str]] = set()
    partial_evidence: dict[str, dict] = {}
    draft_started = False
    draft_closed = False
    draft_buffer = ""
    draft_tail = ""

    async def keep_lease_alive() -> None:
        while True:
            await asyncio.sleep(10)
            task = repository.get_task(task_id)
            if task is None or task["status"] in TERMINAL_STATUSES or task["status"] == "paused":
                return
            runner.renew_lease(task_id, lease_token=lease_token_provider())

    def readable_draft_delta(event: dict) -> str:
        nonlocal draft_started, draft_closed, draft_buffer, draft_tail
        if draft_closed or event.get("type") != "response.output_text.delta":
            return ""
        chunk = str(event.get("delta") or event.get("text") or "")
        if not chunk:
            return ""
        draft_buffer += chunk
        start_marker = "REPORT_DRAFT:"
        end_marker = "FINAL_JSON:"
        if not draft_started:
            start = draft_buffer.find(start_marker)
            if start < 0:
                draft_buffer = draft_buffer[-len(start_marker):]
                return ""
            draft_started = True
            draft_buffer = draft_buffer[start + len(start_marker):]
        pending = draft_tail + draft_buffer
        end = pending.find(end_marker)
        if end >= 0:
            draft_closed = True
            visible = pending[:end]
            draft_buffer = ""
            draft_tail = ""
            return visible
        keep = 0
        for size in range(1, min(len(pending), len(end_marker) - 1) + 1):
            if end_marker.startswith(pending[-size:]):
                keep = size
        visible = pending[:-keep] if keep else pending
        draft_tail = pending[-keep:] if keep else ""
        draft_buffer = ""
        return visible

    try:
        if not await _continue_when_running(runner, task_id, lease_token_provider):
            return
        runner.commit_step(
            task_id,
            lease_token=lease_token_provider(),
            step_id="planning",
            kind="planning",
            step_input={"provider": "deepseek"},
            step_output={"message": "正在理解研究问题并准备联网调查"},
            idempotency_key="deepseek:planning",
            frontier={
                "plan_version": 1,
                "ready_step_ids": ["provider_research"],
                "running_step_ids": [],
                "blocked_step_ids": [],
                "completed_step_ids": ["planning"],
            },
            progress=12,
        )
        task = repository.get_task(task_id)
        if task is None:
            return

        def record_provider_event(event: dict) -> None:
            delta = readable_draft_delta(event)
            if delta:
                current = repository.get_task(task_id)
                if current is not None and current["status"] not in TERMINAL_STATUSES:
                    repository.append_runtime_event(
                        task_id,
                        kind="report.delta",
                        step="writing",
                        progress=max(current["progress"], 84),
                        message="正在生成研究报告",
                        payload={"delta": delta},
                        lease_token=lease_token_provider(),
                    )
            trace = _deepseek_trace(event)
            if trace is None:
                return
            step, progress, message, payload = trace
            if payload.get("url"):
                safe_event_url = _safe_url(str(payload["url"]))
                payload = {**payload, "url": safe_event_url}
                if payload.get("action") == "open_page":
                    message = f"浏览页面：{safe_event_url}"
            if payload.get("action") == "open_page":
                raw_url = str(payload.get("url") or "").strip()
                url, _fragment = urldefrag(raw_url)
                url = _safe_url(url)
                parsed = urlparse(url)
                if parsed.scheme in {"http", "https"} and parsed.netloc and url not in partial_evidence:
                    domain = parsed.netloc.lower().removeprefix("www.")
                    partial_evidence[url] = {
                        "citation_number": len(partial_evidence) + 1,
                        "title": f"{domain} 网页内容",
                        "publisher": domain,
                        "url": url,
                        "source_type": "网页来源",
                        "excerpt": "研究过程中已访问该来源；即使报告整理失败，也可打开原文继续核对。",
                        "agent": "财报分析 Agent",
                    }
                    repository.replace_evidence(
                        task_id,
                        list(partial_evidence.values()),
                        lease_token=lease_token_provider(),
                    )
            signature = (step, message)
            if signature in emitted:
                return
            emitted.add(signature)
            current = repository.get_task(task_id)
            if current is None or current["status"] in TERMINAL_STATUSES:
                return
            repository.append_runtime_event(
                task_id,
                kind="provider.progress",
                step=step,
                progress=max(current["progress"], progress),
                message=message,
                payload=payload,
                lease_token=lease_token_provider(),
            )

        cached_provider_result = repository.get_completed_step_output(
            task_id, "deepseek:provider_research"
        )
        provider_step_id = "provider_research"
        provider_idempotency_key = "deepseek:provider_research"
        provider_step_committed = False
        if not (
            cached_provider_result is not None
            and isinstance(cached_provider_result.get("report"), dict)
            and isinstance(cached_provider_result.get("evidence"), list)
        ):
            cached_provider_result = repository.get_completed_step_output(
                task_id, "deepseek:provider_research:v2"
            )
            provider_step_id = "provider_research_v2"
            provider_idempotency_key = "deepseek:provider_research:v2"
        if (
            cached_provider_result is not None
            and isinstance(cached_provider_result.get("report"), dict)
            and isinstance(cached_provider_result.get("evidence"), list)
        ):
            report = cached_provider_result["report"]
            evidence = cached_provider_result["evidence"]
            provider_step_committed = True
        else:
            if cached_provider_result is not None:
                provider_step_id = "provider_research_v3"
                provider_idempotency_key = "deepseek:provider_research:v3"
            heartbeat = asyncio.create_task(keep_lease_alive())
            try:
                report, evidence = await client.research(task, on_event=record_provider_event)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        if not await _continue_when_running(runner, task_id, lease_token_provider):
            return

        resolved_company = str(report.get("company") or "").strip()
        if task["company"] == "自动识别中" and not resolved_company:
            raise ValueError("无法从研究问题中唯一识别公司，请在问题中补充公司名称")
        if resolved_company and resolved_company != "自动识别中":
            repository.update_task_identity(
                task_id,
                company=resolved_company,
                symbol=str(report.get("symbol") or "").strip() or None,
                market=str(report.get("market") or "OTHER"),
                lease_token=lease_token_provider(),
            )
        if not provider_step_committed:
            runner.commit_step(
                task_id,
                lease_token=lease_token_provider(),
                step_id=provider_step_id,
                kind="provider_research",
                step_input={"provider": "deepseek"},
                step_output={"report": report, "evidence": evidence},
                idempotency_key=provider_idempotency_key,
                frontier={
                    "plan_version": 1,
                    "ready_step_ids": [],
                    "running_step_ids": [],
                    "blocked_step_ids": [],
                    "completed_step_ids": ["planning", provider_step_id],
                },
                progress=94,
            )
        runner.complete_run(
            task_id,
            lease_token=lease_token_provider(),
            result=report,
            evidence=evidence,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        error_message = _safe_error(exc)
        task = repository.get_task(task_id)
        if task is not None and task["status"] not in TERMINAL_STATUSES:
            runner.fail_run(
                task_id,
                lease_token=lease_token_provider(),
                error=error_message,
            )
