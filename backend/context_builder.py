from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.database import Repository, TERMINAL_STATUSES
from backend.redaction import redact_text


@dataclass(frozen=True)
class CaseContext:
    case_id: str | None = None
    company: str | None = None
    summary: str | None = None
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    active_run: dict[str, Any] | None = None
    has_report: bool = False
    report_has_evidence: bool = False


class ContextBuilder:
    def __init__(self, repository: Repository, *, max_turns: int = 8, max_chars: int = 4_000):
        self.repository = repository
        self.max_turns = max(0, max_turns)
        self.max_chars = max(0, max_chars)

    def build(self, case_id: str | None) -> CaseContext:
        if case_id is None:
            return CaseContext()
        case = self.repository.get_case(case_id)
        if case is None:
            raise KeyError(case_id)

        summary_row = self.repository.get_case_summary(case_id)
        summary = redact_text(summary_row["summary"])[:1_500] if summary_row else None
        raw_turns = self.repository.list_conversation_turns(case_id, limit=self.max_turns)
        remaining = self.max_chars
        selected_reversed: list[dict[str, Any]] = []
        for turn in reversed(raw_turns):
            if remaining <= 0:
                break
            content = redact_text(str(turn["content"]))[:remaining]
            selected_reversed.append({
                "sequence": turn["sequence"],
                "role": turn["role"],
                "content": content,
                "intent": turn["intent"],
            })
            remaining -= len(content)
        recent_turns = list(reversed(selected_reversed))

        pending = self.repository.get_pending_confirmation(case_id)
        pending_projection = None
        if pending is not None:
            pending_projection = {
                "id": pending["id"],
                "kind": pending["kind"],
                "prompt": redact_text(pending["prompt"])[:500],
            }

        active = self.repository.get_latest_task_for_case(
            case_id,
            statuses={"running", "pause_requested", "paused", "resuming"},
        )
        active_projection = None
        if active is not None:
            active_projection = {
                "id": active["id"],
                "status": active["status"],
                "current_step": active["current_step"],
                "progress": active["progress"],
            }
        latest = self.repository.get_latest_task_for_case(case_id)
        has_report = bool(latest and latest["status"] == "completed" and latest["result"])
        report_has_evidence = bool(has_report and latest["evidence"])

        return CaseContext(
            case_id=case_id,
            company=case["company"],
            summary=summary,
            recent_turns=recent_turns,
            pending_confirmation=pending_projection,
            active_run=active_projection,
            has_report=has_report,
            report_has_evidence=report_has_evidence,
        )
