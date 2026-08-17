"""Real filing data source: Cninfo (巨潮资讯) announcement API.

Cninfo is the designated disclosure platform for Shenzhen/Shanghai listed
companies. Its ``hisAnnouncement/query`` endpoint returns structured JSON
(announcement title, security code, timestamp, PDF path) without an API key,
so ``search_filings`` can surface actual official filings instead of relying
on web search plus a domain allowlist.

The endpoint is an unofficial-but-widely-used HTTP API: it can rate-limit or
change shape, so callers must treat this as a best-effort primary source and
degrade to web search on any failure (see ``backend.web_search.search_filings``).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.redaction import redact_url
from backend.web_search import SearchHitData

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_BASE = "http://static.cninfo.com.cn"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "http://www.cninfo.com.cn/new/fulltextSearch",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# Filing-document categories (cninfo category_* values) mapped from the
# planner's document_types vocabulary.
CATEGORY_BY_DOCUMENT_TYPE: dict[str, str] = {
    "annual_report": "category_ndbg_szsh",
    "interim_report": "category_bndbg_szsh",
    "q1_report": "category_yjdbg_szsh",
    "q3_report": "category_sjdbg_szsh",
}

CATEGORY_ALIASES: dict[str, str] = {
    "年报": "annual_report",
    "年度报告": "annual_report",
    "半年报": "interim_report",
    "中报": "interim_report",
    "一季报": "q1_report",
    "三季报": "q3_report",
}


class FilingSourceError(RuntimeError):
    """Raised when a filing source cannot return a usable result."""


def _parse_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and re.fullmatch(r"\d{10,13}", value):
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


class CninfoFilingsSource:
    """Client for the cninfo announcement query endpoint."""

    def __init__(
        self,
        *,
        query_url: str = QUERY_URL,
        static_base: str = STATIC_BASE,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.query_url = query_url
        self.static_base = static_base.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _announcement_url(self, adjunct_url: str) -> str:
        """The cninfo PDF path is relative to the static file host."""
        cleaned = adjunct_url.strip().lstrip("/")
        if not cleaned:
            return ""
        url = f"{self.static_base}/{cleaned}"
        parsed = urlparse(url)
        return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""

    async def search(
        self,
        *,
        company: str | None = None,
        symbol: str | None = None,
        document_types: list[str] | None = None,
        max_results: int = 20,
    ) -> list[SearchHitData]:
        query = company or symbol or ""
        if not query.strip():
            return []
        categories: list[str] = []
        for raw in document_types or []:
            canonical = CATEGORY_ALIASES.get(raw.strip(), raw.strip())
            mapped = CATEGORY_BY_DOCUMENT_TYPE.get(canonical)
            if mapped and mapped not in categories:
                categories.append(mapped)
        payload = {
            "pageNum": "1",
            "pageSize": str(min(max(int(max_results), 1), 50)),
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": symbol or "",
            "searchkey": query,
            "secid": "",
            "category": ",".join(categories),
            "trade": "",
            "seDate": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.post(
                    self.query_url, data=payload, headers=_HEADERS,
                )
        except httpx.HTTPError as exc:
            raise FilingSourceError(f"cninfo request failed: {exc}") from exc
        if response.status_code >= 400:
            raise FilingSourceError(
                f"cninfo request failed (HTTP {response.status_code}): {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FilingSourceError("cninfo response is not valid JSON") from exc
        announcements = body.get("announcements") if isinstance(body, dict) else None
        if not isinstance(announcements, list):
            raise FilingSourceError("cninfo response is missing the announcements list")

        hits: list[SearchHitData] = []
        seen: set[str] = set()
        for announcement in announcements:
            if not isinstance(announcement, dict):
                continue
            # Cninfo wraps matched keywords in <em> highlight tags.
            title = re.sub(r"<[^>]+>", "", str(announcement.get("announcementTitle") or "")).strip()
            adjunct_url = str(announcement.get("adjunctUrl") or "").strip()
            url = self._announcement_url(adjunct_url)
            if not title or not url or url in seen:
                continue
            seen.add(url)
            security = " ".join(part for part in (
                re.sub(r"<[^>]+>", "", str(announcement.get("secName") or "")).strip(),
                str(announcement.get("secCode") or "").strip(),
            ) if part)
            published = _parse_timestamp(announcement.get("announcementTime"))
            snippet = f"{security} 公告".strip()
            if published:
                snippet = f"{snippet}，披露时间 {published[:10]}".strip()
            hits.append(SearchHitData(
                url=redact_url(url),
                title=title[:500],
                snippet=snippet[:1_200],
                publisher="巨潮资讯",
            ))
            if len(hits) >= max_results:
                break
        return hits
