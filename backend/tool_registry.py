from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ToolRegistryError(RuntimeError):
    pass


class ToolTimeout(ToolRegistryError):
    pass


class GenericToolInput(BaseModel):
    # Compatibility schema for third-party/test tools. Production tools below
    # each override this with a fail-closed contract.
    model_config = ConfigDict(extra="allow")
    company: str | None = Field(default=None, max_length=120)
    symbol: str | None = Field(default=None, max_length=32)
    market: str | None = Field(default=None, max_length=16)
    question: str | None = Field(default=None, max_length=2_000)


class SearchFilingsInput(GenericToolInput):
    model_config = ConfigDict(extra="forbid")
    document_types: list[str] = Field(default_factory=list, max_length=20)


class SearchWebInput(GenericToolInput):
    model_config = ConfigDict(extra="forbid")
    domains: list[str] = Field(default_factory=list, max_length=30)
    max_results: int = Field(default=8, ge=1, le=50)
    query: str | None = Field(default=None, max_length=2_000)
    reason: str | None = Field(default=None, max_length=500)


class ReadDocumentInput(GenericToolInput):
    model_config = ConfigDict(extra="forbid")
    selection: str = Field(default="top_authoritative", max_length=80)
    version_ids: list[str] = Field(default_factory=list, max_length=20)


class DocumentTextInput(BaseModel):
    """A document excerpt supplied to extraction tools, bound to its source."""

    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)


class ExtractFinancialFactsInput(GenericToolInput):
    model_config = ConfigDict(extra="forbid")
    periods: int = Field(default=3, ge=1, le=20)
    texts: list[DocumentTextInput] = Field(default_factory=list, max_length=100)


class FinancialFactInput(BaseModel):
    """One financial statement line item supplied to the metrics calculator.

    Mirrors FinancialFact but without a required source binding, so callers can
    pass extracted figures directly. Values may arrive as strings from
    extraction pipelines ("1,234.5") and are normalised by the calculator.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    value: float | int | str
    period: str = Field(min_length=1, max_length=40)
    unit: str = Field(default="", max_length=20)
    currency: str | None = Field(default=None, max_length=16)


class CalculateFinancialMetricsInput(GenericToolInput):
    model_config = ConfigDict(extra="forbid")
    metrics: list[str] = Field(default_factory=list, max_length=30)
    facts: list[FinancialFactInput] = Field(default_factory=list, max_length=500)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "empty", "insufficient"] = "ok"
    data: dict[str, Any] | list[Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    degraded: bool = False
    degraded_reason: str | None = Field(default=None, max_length=500)
    fallback_used: str | None = Field(default=None, max_length=80)


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    publisher: str | None = Field(default=None, max_length=200)
    page: int | None = Field(default=None, ge=1)


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    title: str
    url: str
    snippet: str = ""


class SearchToolResult(ToolResult):
    data: list[SearchHit] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100)


class GetQuoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=32)
    market: str = Field(default="", max_length=16)


class QuoteItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    name: str
    price: float | None = None
    change: float | None = None
    change_pct: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: float | None = None
    turnover: float | None = None
    turnover_rate: float | None = None
    pe: float | None = None
    pb: float | None = None
    total_market_cap: float | None = None
    time: str | None = None
    source: str


class QuoteResult(ToolResult):
    data: list[QuoteItem] = Field(default_factory=list)


class FetchFinancialStatementsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=32)
    market: str = Field(default="", max_length=16)
    periods: int = Field(default=4, ge=1, le=20)


class StatementMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    value: float
    unit: str = Field(default="raw", max_length=20)
    source_field: str = Field(min_length=1, max_length=80)
    report_period: str | None = None


class StatementRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: str | None = None
    report_type: str | None = None
    notice_date: str | None = None
    currency: str = "CNY"
    unit: str = "raw"
    metrics: dict[str, StatementMetric] = Field(default_factory=dict)


class FinancialStatementsResult(ToolResult):
    data: list[StatementRow] = Field(default_factory=list)
    coverage: Literal["a_share", "unsupported"] = "a_share"
    source_url: str | None = None


class DocumentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    heading: str | None = None
    page: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=100_000)


class ReadDocumentResult(ToolResult):
    data: list[DocumentSection] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100)


class FinancialFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: float | int | str
    period: str
    unit: str
    currency: str | None = None
    source_id: str


class FinancialFactsResult(ToolResult):
    data: list[FinancialFact] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100)


class FinancialMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: float | int
    formula: str
    unit: str
    input_fact_ids: list[str] = Field(default_factory=list)


class FinancialMetricsResult(ToolResult):
    data: list[FinancialMetric] = Field(default_factory=list)


class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str
    document_id: str
    text: str
    dense_score: float
    sparse_score: float
    fused_score: float
    source_id: str
    page: int | None = Field(default=None, ge=1)


class RetrievalMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["milvus"]
    mode: Literal["hybrid", "dense_only", "bm25_only"]
    fusion: Literal["rrf", "weighted"]
    sparse: Literal["bm25"]
    dense_model_version: str | None = None
    index_version: str | None = None


class HybridRetrievalInput(GenericToolInput):
    model_config = ConfigDict(extra="forbid")
    retrieval_mode: Literal["hybrid"]
    fusion: Literal["rrf", "weighted"] = "rrf"
    top_k: int = Field(default=10, ge=1, le=100)


class HybridRetrievalResult(ToolResult):
    data: list[RetrievalHit] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100)
    retrieval: RetrievalMetadata

    @model_validator(mode="after")
    def require_versions_for_success(self):
        if self.status == "ok" and self.data and (
            not self.retrieval.dense_model_version or not self.retrieval.index_version
        ):
            raise ValueError("successful retrieval hits require dense and index versions")
        return self


ToolHandler = Callable[[BaseModel], Awaitable[dict[str, Any]] | dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    risk_level: Literal["low", "medium", "high"]
    timeout_seconds: float
    max_retries: int
    idempotent: bool
    cost_class: int
    requires_confirmation: bool
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    max_output_chars: int = 100_000


@dataclass(frozen=True)
class ToolExecution:
    spec: ToolSpec
    output: dict[str, Any]
    duration_ms: int


@dataclass(frozen=True)
class ToolInvocationContext:
    run_id: str
    plan_version: int
    step_id: str
    idempotency_key: str


class ToolRegistry:
    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool is already registered: {spec.name}")
        if not spec.idempotent and spec.max_retries > 0:
            raise ValueError("non-idempotent tools cannot enable automatic retries")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> set[str]:
        return set(self._specs)

    async def execute(
        self, name: str, payload: dict[str, Any], *,
        context: ToolInvocationContext | None = None,
    ) -> ToolExecution:
        spec = self.get(name)
        if spec is None:
            raise ToolRegistryError(f"unknown tool: {name}")
        try:
            validated_input = spec.input_model.model_validate(payload)
        except ValidationError as exc:
            raise ToolRegistryError(f"invalid tool input: {exc}") from exc
        started = perf_counter()
        async def invoke_once():
            handler = self._handlers[name]
            args = (validated_input, context) if len(inspect.signature(handler).parameters) >= 2 else (validated_input,)
            if inspect.iscoroutinefunction(handler):
                return await handler(*args)
            result = await asyncio.to_thread(handler, *args)
            if inspect.isawaitable(result):
                return await result
            return result

        raw = None
        last_error: BaseException | None = None
        for attempt in range(spec.max_retries + 1):
            try:
                raw = await asyncio.wait_for(invoke_once(), timeout=spec.timeout_seconds)
                last_error = None
                break
            except TimeoutError as exc:
                last_error = ToolTimeout(f"tool timed out: {name}")
            except Exception as exc:
                last_error = exc
            if attempt < spec.max_retries:
                await asyncio.sleep(min(0.05 * (2 ** attempt), 0.5))
        if last_error is not None:
            if isinstance(last_error, ToolRegistryError):
                raise last_error
            raise ToolRegistryError(f"tool execution failed: {name}") from last_error
        try:
            validated_output = spec.output_model.model_validate(raw)
        except ValidationError as exc:
            raise ToolRegistryError(f"invalid tool output: {exc}") from exc
        output = validated_output.model_dump()
        if len(str(output)) > spec.max_output_chars:
            raise ToolRegistryError(f"tool output exceeds limit: {name}")
        return ToolExecution(
            spec=spec,
            output=output,
            duration_ms=max(0, int((perf_counter() - started) * 1_000)),
        )


async def _unconfigured_hybrid(_payload: BaseModel) -> dict[str, Any]:
    return {
        "status": "empty", "data": [], "evidence": [], "degraded": True,
        "degraded_reason": "Milvus hybrid adapter is not configured until Phase 4",
        "fallback_used": None,
        "retrieval": {
            "backend": "milvus", "mode": "hybrid", "fusion": "rrf",
            "sparse": "bm25", "dense_model_version": None, "index_version": None,
        },
    }


def build_default_registry(
    *,
    retrieval_handler: ToolHandler | None = None,
    read_document_handler: ToolHandler | None = None,
    search_handlers: dict[str, ToolHandler] | None = None,
) -> ToolRegistry:
    from backend.fact_extraction import extract_financial_facts
    from backend.financial_metrics import calculate_financial_metrics
    from backend.quote_source import get_quote
    from backend.read_document import read_document_unconfigured
    from backend.web_search import search_filings, search_web

    registry = ToolRegistry()

    def wired(name: str, handler: ToolHandler) -> ToolHandler:
        if search_handlers and name in search_handlers:
            return search_handlers[name]
        return handler

    registry.register(
        ToolSpec(
            name="extract_financial_facts", version="1", risk_level="low",
            timeout_seconds=10, max_retries=1, idempotent=True, cost_class=3,
            requires_confirmation=False, input_model=ExtractFinancialFactsInput,
            output_model=FinancialFactsResult,
        ),
        extract_financial_facts,
    )
    registry.register(
        ToolSpec(
            name="read_document", version="1", risk_level="low",
            timeout_seconds=10, max_retries=1, idempotent=True, cost_class=2,
            requires_confirmation=False, input_model=ReadDocumentInput,
            output_model=ReadDocumentResult,
        ),
        read_document_handler or read_document_unconfigured,
    )
    registry.register(
        ToolSpec(
            name="search_web", version="1", risk_level="low",
            timeout_seconds=60, max_retries=2, idempotent=True, cost_class=3,
            requires_confirmation=False, input_model=SearchWebInput,
            output_model=SearchToolResult,
        ),
        wired("search_web", search_web),
    )
    registry.register(
        ToolSpec(
            name="search_filings", version="1", risk_level="low",
            timeout_seconds=60, max_retries=2, idempotent=True, cost_class=2,
            requires_confirmation=False, input_model=SearchFilingsInput,
            output_model=SearchToolResult,
        ),
        wired("search_filings", search_filings),
    )
    registry.register(
        ToolSpec(
            name="calculate_financial_metrics", version="1", risk_level="low",
            timeout_seconds=10, max_retries=1, idempotent=True, cost_class=1,
            requires_confirmation=False, input_model=CalculateFinancialMetricsInput,
            output_model=FinancialMetricsResult,
        ),
        calculate_financial_metrics,
    )
    registry.register(
        ToolSpec(
            name="get_quote", version="1", risk_level="low",
            timeout_seconds=15, max_retries=2, idempotent=True, cost_class=1,
            requires_confirmation=False, input_model=GetQuoteInput,
            output_model=QuoteResult,
        ),
        get_quote,
    )
    from backend.financial_statements import fetch_financial_statements
    registry.register(
        ToolSpec(
            name="fetch_financial_statements", version="1", risk_level="low",
            timeout_seconds=15, max_retries=2, idempotent=True, cost_class=2,
            requires_confirmation=False, input_model=FetchFinancialStatementsInput,
            output_model=FinancialStatementsResult,
        ),
        fetch_financial_statements,
    )
    registry.register(
        ToolSpec(
            name="retrieve_documents", version="1", risk_level="low",
            timeout_seconds=10, max_retries=1, idempotent=True, cost_class=2,
            requires_confirmation=False, input_model=HybridRetrievalInput,
            output_model=HybridRetrievalResult,
        ),
        retrieval_handler or _unconfigured_hybrid,
    )
    return registry
