import asyncio
import time

import pytest

from backend.tool_registry import (
    GenericToolInput,
    HybridRetrievalInput,
    HybridRetrievalResult,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    ToolTimeout,
    build_default_registry,
)


def spec(name="test", *, timeout=1, output_model=ToolResult, max_output=100_000):
    return ToolSpec(
        name=name, version="1", risk_level="low", timeout_seconds=timeout,
        max_retries=1, idempotent=True, cost_class=1,
        requires_confirmation=False, input_model=GenericToolInput,
        output_model=output_model, max_output_chars=max_output,
    )


def test_default_registry_contains_seven_contracts_and_hybrid_milvus_metadata():
    registry = build_default_registry()
    assert registry.names() == {
        "search_filings", "search_web", "retrieve_documents", "read_document",
        "extract_financial_facts", "calculate_financial_metrics", "get_quote",
        "fetch_financial_statements",
    }
    retrieval = registry.get("retrieve_documents")
    assert retrieval.input_model is HybridRetrievalInput
    assert retrieval.output_model is HybridRetrievalResult
    execution = asyncio.run(registry.execute(
        "retrieve_documents",
        {"company": "腾讯控股", "retrieval_mode": "hybrid", "fusion": "rrf", "top_k": 10},
    ))
    assert execution.output["retrieval"]["backend"] == "milvus"
    assert execution.output["retrieval"]["sparse"] == "bm25"
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute("search_filings", {"unexpected": "value"}))
    invalid_output = ToolRegistry()
    invalid_output.register(spec(), lambda _payload: {"status": "ok", "unexpected": True})
    with pytest.raises(ToolRegistryError, match="invalid tool output"):
        asyncio.run(invalid_output.execute("test", {}))


def test_registry_validates_input_output_unknown_and_duplicates():
    registry = ToolRegistry()
    registry.register(spec(), lambda _payload: {"status": "ok", "data": {"x": 1}})
    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec(), lambda _payload: {})
    with pytest.raises(ToolRegistryError, match="unknown"):
        asyncio.run(registry.execute("missing", {}))
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute("test", {"company": "x" * 121}))

    invalid = ToolRegistry()
    invalid.register(spec(), lambda _payload: {"status": "not-valid"})
    with pytest.raises(ToolRegistryError, match="invalid tool output"):
        asyncio.run(invalid.execute("test", {}))


def test_registry_enforces_timeout_and_output_limit():
    async def slow(_payload):
        await asyncio.sleep(0.03)
        return {"status": "ok"}

    registry = ToolRegistry()
    registry.register(spec(timeout=0.001), slow)
    with pytest.raises(ToolTimeout):
        asyncio.run(registry.execute("test", {}))

    large = ToolRegistry()
    large.register(spec(max_output=30), lambda _payload: {"status": "ok", "data": {"x": "a" * 100}})
    with pytest.raises(ToolRegistryError, match="exceeds"):
        asyncio.run(large.execute("test", {}))


def test_sync_handler_runs_off_loop_and_retry_limit_is_enforced():
    attempts = []

    def flaky(_payload):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("temporary")
        return {"status": "ok", "data": {"attempts": len(attempts)}}

    registry = ToolRegistry()
    registry.register(spec(), flaky)
    result = asyncio.run(registry.execute("test", {}))
    assert result.output["data"]["attempts"] == 2

    slow = ToolRegistry()
    slow.register(spec(timeout=0.001), lambda _payload: (time.sleep(0.02) or {"status": "ok"}))
    with pytest.raises(ToolTimeout):
        asyncio.run(slow.execute("test", {}))


def test_non_idempotent_tool_cannot_enable_automatic_retries():
    invalid = spec()
    invalid = ToolSpec(**{**invalid.__dict__, "idempotent": False, "max_retries": 1})
    with pytest.raises(ValueError, match="non-idempotent"):
        ToolRegistry().register(invalid, lambda _payload: {"status": "ok"})
