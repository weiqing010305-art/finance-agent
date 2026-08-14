from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import re
from typing import Iterable

from backend.chunking import chunk_sections
from backend.database import Repository
from backend.schemas import DocumentChunk, DocumentSource, ParsedSection
from backend.redaction import redact_url


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


class _TextHTMLParser(HTMLParser):
    block_tags = {"article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_document(content: bytes | str, mime_type: str) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("document is empty or exceeds size limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("document must be valid UTF-8") from exc
    if mime_type.lower().split(";", 1)[0] in {"text/html", "application/xhtml+xml"}:
        parser = _TextHTMLParser()
        parser.feed(text)
        text = "".join(parser.parts)
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("document contains no indexable text")
    return text


def parse_sections(text: str) -> list[ParsedSection]:
    lines = text.splitlines(keepends=True)
    sections: list[ParsedSection] = []
    heading: str | None = None
    body: list[str] = []
    body_start = 0
    offset = 0

    def flush(end: int) -> None:
        nonlocal body
        value = "".join(body).strip()
        if value:
            leading = len("".join(body)) - len("".join(body).lstrip())
            sections.append(
                ParsedSection(
                    heading=heading,
                    text=value,
                    char_start=body_start + leading,
                    char_end=end,
                )
            )
        body = []

    for line in lines:
        stripped = line.strip()
        is_heading = bool(re.match(r"^#{1,6}\s+\S", stripped)) or (
            0 < len(stripped) <= 60 and stripped.endswith(("：", ":"))
        )
        if is_heading:
            flush(offset)
            heading = re.sub(r"^#{1,6}\s+", "", stripped).rstrip("：:")
            body_start = offset + len(line)
        else:
            if not body:
                body_start = offset
            body.append(line)
        offset += len(line)
    flush(len(text))
    if not sections:
        sections = [ParsedSection(text=text, char_start=0, char_end=len(text))]
    return sections


def ingest_document(
    repository: Repository,
    source: DocumentSource,
    content: bytes | str,
    *,
    embedding_profile_id: str,
    index_version: str,
    max_chars: int = 1200,
    overlap_chars: int = 120,
) -> tuple[dict, list[DocumentChunk], bool]:
    normalized = normalize_document(content, source.mime_type)
    canonical_source_uri = redact_url(source.source_uri)
    source = source.model_copy(update={"source_uri": canonical_source_uri})
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    version_id = "ver_" + hashlib.sha256(
        f"{canonical_source_uri}:{source.access_scope}:{digest}".encode("utf-8")
    ).hexdigest()[:32]
    chunks = chunk_sections(
        version_id,
        parse_sections(normalized),
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )
    result, created = repository.persist_document_ingestion(
        source=source,
        normalized_text=normalized,
        content_sha256=digest,
        version_id=version_id,
        chunks=chunks,
        embedding_profile_id=embedding_profile_id,
        index_version=index_version,
    )
    return result, chunks, created
