from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


TaskStatus = Literal[
    "running",
    "pause_requested",
    "paused",
    "resuming",
    "completed",
    "failed",
]

Intent = Literal[
    "SOCIAL_ACK",
    "CONTROL",
    "CONFIRMATION",
    "REPORT_QA",
    "RESEARCH_FOLLOWUP",
    "RESEARCH_NEW",
    "CLARIFICATION",
    "OUT_OF_SCOPE",
    "AMBIGUOUS",
]

EntityResolutionStatus = Literal["resolved", "ambiguous", "unresolved"]
ResearchIntakeStatus = Literal[
    "awaiting_confirmation", "ready", "needs_clarification", "running", "failed"
]


class RouteDecision(BaseModel):
    intent: Intent
    confidence: float = Field(ge=0, le=1)
    case_id: str | None = None
    requires_planner: bool
    external_research_allowed: bool
    response_policy: str
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_permission_invariants(self):
        research_intent = self.intent in {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"}
        if self.requires_planner != research_intent:
            raise ValueError("planner access must match a research intent")
        if self.external_research_allowed:
            raise ValueError("the routing layer cannot grant external research permission")
        return self


class ConversationRouteRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2_000)
    case_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value):
        return value.strip() if isinstance(value, str) else value


class ConversationRouteResponse(BaseModel):
    request_id: str
    case_id: str | None = None
    decision: RouteDecision
    response: str
    trace: list[str] = Field(default_factory=list)


class SecurityCandidate(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    company: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=32)
    market: Literal["CN", "HK", "US", "OTHER"]
    security_type: str = Field(default="equity", max_length=32)
    confidence: float = Field(ge=0, le=1)
    matched_alias: str = Field(min_length=1, max_length=120)


class EntityResolution(BaseModel):
    status: EntityResolutionStatus
    query: str = Field(min_length=1, max_length=240)
    candidates: list[SecurityCandidate] = Field(default_factory=list, max_length=10)
    selected: SecurityCandidate | None = None
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_resolution(self):
        if self.status == "resolved" and self.selected is None:
            raise ValueError("resolved entity requires selected candidate")
        if self.status != "resolved" and self.selected is not None:
            raise ValueError("only resolved entity may contain selected candidate")
        if self.status == "ambiguous" and len(self.candidates) < 2:
            raise ValueError("ambiguous entity requires at least two candidates")
        if self.status == "unresolved" and self.candidates:
            raise ValueError("unresolved entity cannot contain candidates")
        return self


class ResearchIntakeStartRequest(BaseModel):
    route_request_id: str = Field(min_length=1, max_length=128)
    depth: Literal["quick", "standard", "deep"] = "standard"
    budget_limit: int = Field(default=20, ge=1, le=10_000)


class EntityConfirmationRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=128)


class ResearchIntakeView(BaseModel):
    id: str
    route_request_id: str
    message: str
    depth: Literal["quick", "standard", "deep"]
    budget_limit: int
    status: ResearchIntakeStatus
    entity_query: str | None = None
    candidates: list[SecurityCandidate] = Field(default_factory=list)
    resolved_entity: SecurityCandidate | None = None
    confirmation_id: str | None = None
    run_id: str | None = None
    replan_count: int = 0
    created_at: str
    updated_at: str


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
