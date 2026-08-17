"""Controlled document reading tool.

``ReadDocumentTool`` reads persisted document versions through the
document-domain repository (``document_versions.normalized_text``) and
returns parsed sections with page and heading references. The model never
sees the raw document store: it only receives bounded, sectioned excerpts
through this registered read-only tool.

Without an injected document repository (e.g. in the default registry) the
tool degrades explicitly instead of failing the run.
"""

from __future__ import annotations

from typing import Any

from backend.db.document_repository import DocumentRepository
from backend.documents import parse_sections
from backend.tool_registry import (
    DocumentSection,
    ReadDocumentInput,
    ReadDocumentResult,
)

MAX_SECTIONS_PER_DOCUMENT = 5
MAX_SECTION_CHARS = 10_000


class ReadDocumentTool:
    def __init__(self, repository: DocumentRepository) -> None:
        self.repository = repository

    async def __call__(
        self,
        payload: ReadDocumentInput,
        context: Any = None,
    ) -> dict[str, Any]:
        if payload.version_ids:
            versions = [
                row for row in (
                    self.repository.get_document_version(version_id)
                    for version_id in payload.version_ids[:20]
                )
                if row is not None
            ]
        else:
            versions = self.repository.list_document_versions(
                company=payload.company,
                market=payload.market,
                access_scope="public",
                limit=10,
            )
        if not versions:
            return {
                "status": "empty",
                "data": [],
                "evidence": [],
                "degraded": True,
                "degraded_reason": "no persisted documents found for the confirmed entity",
                "fallback_used": None,
            }

        sections: list[DocumentSection] = []
        for version in versions:
            text = str(version.get("normalized_text") or "")
            if not text:
                continue
            parsed = parse_sections(text)
            if payload.selection == "all":
                selected = parsed
            else:  # top_authoritative and any unknown selection default to the head
                selected = parsed[:MAX_SECTIONS_PER_DOCUMENT]
            for section in selected:
                sections.append(DocumentSection(
                    source_id=str(version["id"]),
                    heading=section.heading,
                    page=section.page,
                    text=section.text[:MAX_SECTION_CHARS],
                ))
        if not sections:
            return {
                "status": "empty",
                "data": [],
                "evidence": [],
                "degraded": True,
                "degraded_reason": "persisted documents contain no readable sections",
                "fallback_used": None,
            }
        return {
            "status": "ok",
            "data": [section.model_dump() for section in sections],
            "evidence": [],
            "degraded": False,
            "degraded_reason": None,
            "fallback_used": None,
        }


async def read_document_unconfigured(
    payload: ReadDocumentInput,
    context: Any = None,
) -> dict[str, Any]:
    return {
        "status": "empty",
        "data": [],
        "evidence": [],
        "degraded": True,
        "degraded_reason": "document repository is not configured for this runtime",
        "fallback_used": None,
    }
