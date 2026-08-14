from __future__ import annotations

import hashlib

from backend.schemas import DocumentChunk, ParsedSection


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def chunk_sections(
    document_version_id: str,
    sections: list[ParsedSection],
    *,
    max_chars: int = 1200,
    overlap_chars: int = 120,
) -> list[DocumentChunk]:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be between 0 and max_chars")
    chunks: list[DocumentChunk] = []
    ordinal = 0
    for section in sections:
        local_start = 0
        while local_start < len(section.text):
            local_end = min(local_start + max_chars, len(section.text))
            if local_end < len(section.text):
                natural_break = max(
                    section.text.rfind("\n", local_start, local_end),
                    section.text.rfind("。", local_start, local_end),
                    section.text.rfind("；", local_start, local_end),
                )
                if natural_break > local_start + max_chars // 2:
                    local_end = natural_break + 1
            raw_text = section.text[local_start:local_end]
            leading = len(raw_text) - len(raw_text.lstrip())
            trailing = len(raw_text) - len(raw_text.rstrip())
            text = raw_text.strip()
            if text:
                absolute_start = section.char_start + local_start + leading
                absolute_end = section.char_start + local_end - trailing
                identity = f"{document_version_id}:{ordinal}:{absolute_start}:{sha256_text(text)}"
                chunks.append(
                    DocumentChunk(
                        id="chk_" + sha256_text(identity)[:32],
                        document_version_id=document_version_id,
                        ordinal=ordinal,
                        text=text,
                        content_sha256=sha256_text(text),
                        char_start=absolute_start,
                        char_end=absolute_end,
                        section=section.heading,
                        page=section.page,
                    )
                )
                ordinal += 1
            if local_end >= len(section.text):
                break
            local_start = max(local_start + 1, local_end - overlap_chars)
    return chunks
