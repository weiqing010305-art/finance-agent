from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class StrictDomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentSource(StrictDomainModel):
    source_uri: str = Field(min_length=1, max_length=2048)
    source_type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=200)
    access_scope: str = Field(default="public", min_length=1, max_length=128)
    company: str | None = Field(default=None, max_length=120)
    symbol: str | None = Field(default=None, max_length=32)
    market: str | None = Field(default=None, max_length=32)
    source_version: str | None = Field(default=None, max_length=128)
    mime_type: str = Field(default="text/plain", min_length=1, max_length=128)
    published_at: str | None = None


class ParsedSection(StrictDomainModel):
    heading: str | None = Field(default=None, max_length=500)
    text: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_offsets(self):
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        return self


class DocumentChunk(StrictDomainModel):
    id: str = Field(min_length=1, max_length=128)
    document_version_id: str = Field(min_length=1, max_length=128)
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    section: str | None = Field(default=None, max_length=500)
    page: int | None = Field(default=None, ge=1)


class EvidenceItem(StrictDomainModel):
    id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    source_uri: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=64)
    excerpt: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_scope: str = Field(min_length=1, max_length=128)
    authority_tier: int = Field(ge=0, le=5)
    retrieved_at: str
    published_at: str | None = None
    document_version_id: str | None = None
    chunk_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    company: str | None = None
    period: str | None = None


class VerifiedClaim(StrictDomainModel):
    id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    status: Literal["supported", "partially_supported", "unsupported", "conflicted"]
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    period: str | None = None
    unit: str | None = None
    currency: str | None = None


class ClaimCandidate(StrictDomainModel):
    id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    period: str | None = None
    unit: str | None = None
    currency: str | None = None


MemoryType = Literal[
    "company_fact", "entity_identity", "user_preference", "case_summary",
    "task_experience",
]
MemoryStatus = Literal[
    "candidate", "verified", "active", "conflicted", "rejected",
    "superseded", "expired", "deleted",
]
MemoryScopeKind = Literal["public_company", "user", "case", "system"]


class MemoryScope(StrictDomainModel):
    scope_kind: MemoryScopeKind
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    case_id: str | None = Field(default=None, min_length=1, max_length=128)
    company: str | None = Field(default=None, min_length=1, max_length=120)
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    market: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_scope_identity(self):
        if self.scope_kind == "public_company":
            if not self.company or not self.market:
                raise ValueError("public company scope requires company and market")
            if self.user_id or self.case_id:
                raise ValueError("public company scope cannot contain private identity")
        elif self.scope_kind == "user":
            if not self.user_id or self.case_id:
                raise ValueError("user scope requires user_id and forbids case_id")
        elif self.scope_kind == "case":
            if not self.user_id or not self.case_id:
                raise ValueError("case scope requires user_id and case_id")
        elif self.scope_kind == "system" and (self.user_id or self.case_id):
            raise ValueError("system scope cannot contain private identity")
        return self


class MemoryCandidate(StrictDomainModel):
    memory_type: MemoryType
    memory_key: str = Field(min_length=1, max_length=240)
    scope: MemoryScope
    content: dict[str, Any]
    content_text: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(min_length=1, max_length=240)
    confidence: float = Field(ge=0, le=1)
    source_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_summary_id: str | None = Field(default=None, min_length=1, max_length=128)
    claim_ids: list[str] = Field(default_factory=list, max_length=32)
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    explicit_user_confirmation: bool = False
    period: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_write_policy(self):
        if self.memory_type == "company_fact":
            if self.scope.scope_kind != "public_company":
                raise ValueError("company facts require public_company scope")
            if not self.source_run_id or not self.claim_ids or not self.evidence_ids:
                raise ValueError("company facts require persisted claims and evidence")
        elif self.memory_type in {"user_preference", "entity_identity"}:
            if not self.explicit_user_confirmation:
                raise ValueError("explicit user confirmation is required")
            expected = "user" if self.memory_type == "user_preference" else "case"
            if self.scope.scope_kind != expected:
                raise ValueError(f"{self.memory_type} requires {expected} scope")
        elif self.memory_type == "case_summary":
            if self.scope.scope_kind != "case" or not self.source_summary_id:
                raise ValueError("case summaries require case scope and persisted summary")
        elif self.memory_type == "task_experience" and not self.source_run_id:
            raise ValueError("task experience requires a persisted run")
        return self


class MemoryView(StrictDomainModel):
    id: str
    memory_id: str
    version: int = Field(ge=1)
    memory_type: MemoryType
    memory_key: str
    status: MemoryStatus
    scope: MemoryScope
    content: dict[str, Any]
    content_text: str
    confidence: float = Field(ge=0, le=1)
    period: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    created_at: str
    updated_at: str


class MemoryContextItem(StrictDomainModel):
    memory_id: str
    memory_type: MemoryType
    content_text: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    trust_boundary: Literal["untrusted_memory"] = "untrusted_memory"


class DeletionJob(StrictDomainModel):
    id: str
    memory_id: str | None = None
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "claimed", "completed", "failed"]
    idempotency_key: str
    claim_token_hash: str | None = Field(default=None, exclude=True)
    claim_expires_at: str | None = None
    attempt: int = Field(ge=0)
    error: str | None = None
    created_at: str
    updated_at: str


class PreferenceMemoryWrite(StrictDomainModel):
    memory_key: str = Field(min_length=1, max_length=240)
    value: dict[str, Any]
    text: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(min_length=1, max_length=240)


class MemoryDeleteRequest(StrictDomainModel):
    idempotency_key: str = Field(min_length=1, max_length=240)


class ReportSection(StrictDomainModel):
    heading: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)


class ReportDraft(StrictDomainModel):
    company: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1)
    sections: list[ReportSection] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    degraded: bool = False


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


class ResearchIntakeStartResponse(BaseModel):
    intake: "ResearchIntakeView"
    trace: list[str] = Field(default_factory=list)


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


class PlanStep(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["tool", "synthesis"] = "tool"
    tool_name: str = Field(min_length=1, max_length=80)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    input: dict[str, Any] = Field(default_factory=dict)
    success_criteria: list[str] = Field(min_length=1, max_length=10)
    max_attempts: int = Field(default=1, ge=1, le=3)
    estimated_cost: int = Field(default=1, ge=0, le=1_000)


class ResearchPlan(BaseModel):
    version: int = Field(default=1, ge=1)
    goal: str = Field(min_length=5, max_length=2_000)
    steps: list[PlanStep] = Field(min_length=1, max_length=30)
    max_replans: int = Field(default=1, ge=0, le=1)
    fallback_used: bool = False

    @model_validator(mode="after")
    def validate_dag(self):
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            if step.id in step.dependencies:
                raise ValueError("plan step cannot depend on itself")
            missing = set(step.dependencies) - known
            if missing:
                raise ValueError(f"plan dependency does not exist: {sorted(missing)}")
        graph = {step.id: set(step.dependencies) for step in self.steps}
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("plan DAG contains a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in graph[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)
        return self

    @property
    def estimated_cost(self) -> int:
        return sum(step.estimated_cost for step in self.steps)


class AuthorizationDecision(BaseModel):
    allowed: bool
    run_id: str
    plan_version: int = Field(ge=1)
    step_id: str
    tool_name: str
    estimated_cost: int = Field(ge=0)
    budget_before: int = Field(ge=0)
    reason_codes: list[str] = Field(default_factory=list)
    capability_token: str | None = Field(default=None, exclude=True)


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
