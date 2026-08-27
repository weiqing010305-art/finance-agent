"""Controlled web search tool backed by the DeepSeek Responses API.

This replaces the legacy model-direct web access with a registered, read-only
tool: the model requests a search, the tool performs it through the DeepSeek
``web_search`` server-side tool, and results are normalised into SearchHits
with URL safety checks before they ever reach the model. Without a configured
``DEEPSEEK_API_KEY`` the tool degrades explicitly instead of failing the run.

``search_filings`` is the same search restricted to an official-disclosure
domain allowlist, so filings queries can only surface exchange / regulator /
company-IR sources.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.deepseek_research import (
    DEFAULT_DEEPSEEK_MODEL,
    DeepSeekResearchClient,
)
from backend.redaction import redact_url
from backend.tool_registry import (
    SearchFilingsInput,
    SearchHit,
    SearchWebInput,
    SearchToolResult,
)

# ---------------------------------------------------------------------------
# Official-disclosure domain allowlist (exchange / regulator / company IR)
# ---------------------------------------------------------------------------
OFFICIAL_FILING_DOMAINS: tuple[str, ...] = (
    "cninfo.com.cn",     # 巨潮资讯（深交所指定披露平台）
    "sse.com.cn",        # 上海证券交易所
    "szse.cn",           # 深圳证券交易所
    "hkexnews.hk",       # 香港交易所披露易
    "sec.gov",           # 美国证监会 EDGAR
    "hkenumbers.com.hk", # 港交所 e-IPO 相关披露
)


class WebSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchHitData:
    url: str
    title: str
    snippet: str = ""
    publisher: str = ""


def _safe_search_url(value: Any) -> str:
    """Return the URL only if it is a public http(s) page (SSRF / private-IP guard)."""
    cleaned = DeepSeekResearchClient._clean_url(value)
    if not cleaned:
        return ""
    if not DeepSeekResearchClient._is_public_web_url(cleaned):
        return ""
    return cleaned


class DeepSeekWebSearch:
    """Minimal client for the DeepSeek Responses ``web_search`` tool."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DeepSeekWebSearch | None":
        values = os.environ if env is None else env
        key = (values.get("DEEPSEEK_API_KEY") or "").strip()
        if not key:
            return None
        return cls(
            key,
            model=(values.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip(),
            base_url=(values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip(),
        )

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "DeepSeekWebSearch | None":
        """Build a web-search client from per-tenant LLM settings."""
        if not settings:
            return None
        key = str(settings.get("api_key") or "").strip()
        if not key:
            return None
        return cls(
            key,
            model=str(settings.get("model") or DEFAULT_DEEPSEEK_MODEL).strip(),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com").strip(),
        )

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
    ) -> list[SearchHitData]:
        """Perform one web search and return safe, normalised hits."""
        if not query.strip():
            return []
        payload: dict[str, Any] = {
            "model": self.model,
            "input": query[:2_000],
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=timeout,
                transport=self.transport,
            ) as client:
                response = await client.post("/responses", json=payload)
        except httpx.HTTPError as exc:
            raise WebSearchError(f"search request failed: {exc}") from exc
        if response.status_code >= 400:
            raise WebSearchError(
                f"search failed (HTTP {response.status_code}): {response.text[:300]}"
            )
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise WebSearchError("search response is not valid JSON") from exc

        hits: list[SearchHitData] = []
        seen: set[str] = set()
        for item in body.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content", []) or []:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                for annotation in part.get("annotations", []) or []:
                    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                        continue
                    citation = annotation.get("url_citation") if isinstance(annotation.get("url_citation"), dict) else annotation
                    url = _safe_search_url(citation.get("url"))
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    domain = urlparse(url).netloc.lower().removeprefix("www.")
                    hits.append(SearchHitData(
                        url=redact_url(url),
                        title=str(citation.get("title") or url)[:500],
                        snippet=str(citation.get("content") or citation.get("snippet") or "")[:1_200],
                        publisher="",
                    ))
                    if len(hits) >= max_results:
                        return hits
        return hits


def _to_hit_result(hits: list[SearchHitData]) -> dict[str, Any]:
    if not hits:
        return {
            "status": "empty",
            "data": [],
            "evidence": [],
            "degraded": True,
            "degraded_reason": "no search results returned",
            "fallback_used": None,
        }
    data = [
        SearchHit(
            source_id=_safe_search_url(hit.url) or hit.url,
            title=hit.title,
            url=hit.url,
            snippet=hit.snippet,
        ).model_dump()
        for hit in hits
    ]
    evidence = [
        {
            "source_id": hit.url,
            "title": hit.title,
            "url": hit.url,
            "publisher": hit.publisher or urlparse(hit.url).netloc,
        }
        for hit in hits
    ]
    return {
        "status": "ok",
        "data": data,
        "evidence": evidence,
        "degraded": False,
        "degraded_reason": None,
        "fallback_used": None,
    }


def _unavailable_result(reason: str) -> dict[str, Any]:
    return {
        "status": "empty",
        "data": [],
        "evidence": [],
        "degraded": True,
        "degraded_reason": reason[:500],
        "fallback_used": None,
    }


async def search_web(
    payload: SearchWebInput,
    context: Any = None,
    *,
    _client: DeepSeekWebSearch | None = None,
) -> dict[str, Any]:
    context_settings = getattr(context, "llm_settings", None) if context is not None else None
    client = (
        _client
        or DeepSeekWebSearch.from_settings(context_settings)
        or DeepSeekWebSearch.from_env()
    )
    if client is None:
        return _unavailable_result("DEEPSEEK_API_KEY is not configured; web search unavailable")
    query = (payload.query or payload.question or "").strip()
    if not query:
        return _unavailable_result("no query supplied")
    try:
        hits = await client.search(query, max_results=payload.max_results)
    except WebSearchError as exc:
        return _unavailable_result(f"web search failed: {exc}")
    return _to_hit_result(hits)


OFFICIAL_DOMAINS_SUFFIXES = tuple(f".{domain}" for domain in OFFICIAL_FILING_DOMAINS)


def _is_official_filing_domain(domain: str) -> bool:
    lowered = domain.lower().removeprefix("www.")
    return lowered in OFFICIAL_FILING_DOMAINS or lowered.endswith(OFFICIAL_DOMAINS_SUFFIXES)


async def search_filings(
    payload: SearchFilingsInput,
    context: Any = None,
    *,
    _client: DeepSeekWebSearch | None = None,
    _filings_source: Any = None,
) -> dict[str, Any]:
    """Search official filings, real source first, web-search fallback.

    For A-share companies the real cninfo announcement API (巨潮资讯) is the
    primary source: it returns structured official filings without an API key.
    Any failure — no coverage for the market, an API error, or an empty
    result — degrades to the domain-allowlisted web search, which is flagged
    via ``degraded``/``fallback_used`` so callers can tell the difference.
    """
    from backend.filings_source import CninfoFilingsSource, FilingSourceError

    fallback_reason: str | None = None
    primary_covered = payload.market in {None, "", "CN", "A", "CN_A", "SZSE", "SSE"}
    if primary_covered:
        source = _filings_source if _filings_source is not None else CninfoFilingsSource()
        try:
            hits = await source.search(
                company=payload.company,
                symbol=payload.symbol,
                document_types=payload.document_types,
                max_results=20,
            )
            if hits:
                return _to_hit_result(hits)
            fallback_reason = "cninfo returned no announcements for the confirmed entity"
        except FilingSourceError as exc:
            fallback_reason = f"cninfo filings API failed: {exc}"
    else:
        fallback_reason = f"cninfo does not cover market {payload.market}"

    context_settings = getattr(context, "llm_settings", None) if context is not None else None
    client = (
        _client
        or DeepSeekWebSearch.from_settings(context_settings)
        or DeepSeekWebSearch.from_env()
    )
    if client is None:
        return _unavailable_result(
            f"filings source unavailable ({fallback_reason}); "
            "DEEPSEEK_API_KEY is not configured for the web fallback"
        )
    parts = [payload.question or ""]
    if payload.company:
        parts.append(payload.company)
    if payload.document_types:
        parts.append("、".join(payload.document_types))
    parts.append("财报 公告 定期报告")
    query = " ".join(part for part in parts if part.strip()).strip()
    if not query:
        return _unavailable_result("no query supplied")
    try:
        hits = await client.search(query, max_results=50)
    except WebSearchError as exc:
        return _unavailable_result(f"filings search failed: {exc}")

    official = [
        hit for hit in hits
        if _is_official_filing_domain(urlparse(hit.url).netloc)
    ]
    if not official:
        return _unavailable_result(f"no official-disclosure results found ({fallback_reason})")
    result = _to_hit_result(official[:20])
    result["degraded"] = True
    result["degraded_reason"] = f"filings primary source degraded: {fallback_reason}"
    result["fallback_used"] = "web_search"
    return result


def build_search_tools(
    *,
    web_client: DeepSeekWebSearch | None = None,
) -> dict[str, Any]:
    """Expose handlers for registry wiring; clients are injectable in tests."""
    return {
        "search_web": lambda payload, context=None: search_web(payload, context, _client=web_client),
        "search_filings": lambda payload, context=None: search_filings(payload, context, _client=web_client),
    }
