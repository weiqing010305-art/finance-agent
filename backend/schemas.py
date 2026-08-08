from typing import Any, Literal

from pydantic import BaseModel, Field


TaskStatus = Literal[
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
]


class ResearchCreate(BaseModel):
    company: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=5, max_length=2_000)
    symbol: str | None = Field(default=None, max_length=32)
    market: str = Field(default="HK", max_length=32)
    agent: str = Field(default="financial", pattern="^(financial|market|company)$")
    depth: str = Field(default="standard", pattern="^(quick|standard|deep)$")


class FeedbackCreate(BaseModel):
    message: str = Field(min_length=1, max_length=2_000)


class TaskView(BaseModel):
    id: str
    case_id: str
    company: str
    symbol: str | None
    market: str
    question: str
    agent: str
    depth: str
    status: TaskStatus
    current_step: str
    progress: int
    created_at: str
    updated_at: str
    result: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
