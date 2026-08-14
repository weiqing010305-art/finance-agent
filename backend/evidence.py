from __future__ import annotations

import hashlib
from typing import Any

from backend.database import utc_now
from backend.redaction import redact_text, redact_url
from backend.schemas import EvidenceItem


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EvidenceBuilder:
    def build_retrieval_items(
        self, run_id: str, hits: list[dict[str, Any]], *, access_scope: str = "public"
    ) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            excerpt = redact_text(str(hit.get("text") or hit.get("excerpt") or "")).strip()
            source_uri = redact_url(str(hit.get("source_uri") or hit.get("url") or ""))
            if not excerpt or not source_uri:
                continue
            digest = content_hash(excerpt)
            identity = (digest, source_uri)
            if identity in seen:
                continue
            seen.add(identity)
            evidence_id = "ev_" + content_hash(f"{run_id}:{digest}:{source_uri}")[:32]
            items.append(EvidenceItem(
                id=evidence_id, run_id=run_id,
                document_version_id=hit.get("document_version_id") or hit.get("source_id"),
                chunk_id=hit.get("chunk_id"), source_uri=source_uri,
                title=str(hit.get("title") or "未命名来源"),
                publisher=str(hit.get("publisher") or "未知发布者"),
                source_type=str(hit.get("source_type") or "document"),
                excerpt=excerpt, content_sha256=digest, access_scope=access_scope,
                authority_tier=int(hit.get("authority_tier") or 0),
                retrieved_at=str(hit.get("retrieved_at") or utc_now()),
                published_at=hit.get("published_at"), page=hit.get("page"),
                section=hit.get("section"), company=hit.get("company"),
                period=hit.get("period"),
            ))
        return items
