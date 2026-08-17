"""Tests for the controlled document reading tool."""

import asyncio

import pytest

from backend.database import Repository
from backend.db.document_repository import DocumentRepository
from backend.documents import ingest_document
from backend.read_document import ReadDocumentTool, read_document_unconfigured
from backend.schemas import DocumentSource
from backend.tool_registry import (
    ReadDocumentInput,
    ToolRegistryError,
    build_default_registry,
)

CONTENT = (
    "# 经营情况\n"
    "公司 2025 年营业收入 100.5 亿元，同比增长 12%。\n"
    "# 财务风险\n"
    "资产负债率保持稳定。\n"
    "# 管理层讨论\n"
    "管理层认为现金流状况健康。\n"
    "# 其他信息\n"
    "更多细节见附录。\n"
)


def _source(**changes) -> DocumentSource:
    values = {
        "source_uri": "https://static.cninfo.com.cn/example/annual-2025.pdf",
        "source_type": "filing",
        "title": "2025 年年度报告",
        "publisher": "巨潮资讯",
        "access_scope": "public",
        "company": "示例公司",
        "symbol": "000001",
        "market": "CN",
        "mime_type": "text/plain",
    }
    values.update(changes)
    return DocumentSource(**values)


def _ingest(repo: Repository, content: str = CONTENT) -> dict:
    result, _chunks, _created = ingest_document(
        repo,
        _source(),
        content,
        embedding_profile_id="bge-large-zh-v1.5:r1",
        index_version="idx-v1",
    )
    return result


def run_tool(repo: Repository, payload: dict) -> dict:
    tool = ReadDocumentTool(DocumentRepository(repo))
    return asyncio.run(tool(
        ReadDocumentInput.model_validate(payload)
    ))


def test_reads_persisted_document_sections(tmp_path):
    repo = Repository(tmp_path / "read.db")
    repo.initialize()
    version = _ingest(repo)

    output = run_tool(repo, {"company": "示例公司", "market": "CN"})
    assert output["status"] == "ok"
    assert not output["degraded"]
    assert output["data"]
    for section in output["data"]:
        assert section["source_id"] == version["id"]
        assert section["heading"] is not None
        assert section["text"]


def test_reads_by_explicit_version_ids(tmp_path):
    repo = Repository(tmp_path / "read-ids.db")
    repo.initialize()
    version = _ingest(repo)

    output = run_tool(repo, {"version_ids": [version["id"]]})
    assert output["status"] == "ok"
    assert all(section["source_id"] == version["id"] for section in output["data"])


def test_unknown_version_id_degrades(tmp_path):
    repo = Repository(tmp_path / "read-missing.db")
    repo.initialize()

    output = run_tool(repo, {"version_ids": ["ver_does_not_exist"]})
    assert output["status"] == "empty"
    assert output["degraded"] is True


def test_no_documents_for_company_degrades(tmp_path):
    repo = Repository(tmp_path / "read-other.db")
    repo.initialize()
    _ingest(repo)

    output = run_tool(repo, {"company": "另一家公司", "market": "CN"})
    assert output["status"] == "empty"
    assert "no persisted documents" in output["degraded_reason"]


def test_top_authoritative_limits_sections(tmp_path):
    repo = Repository(tmp_path / "read-limit.db")
    repo.initialize()
    _ingest(repo)

    output = run_tool(repo, {"company": "示例公司", "selection": "top_authoritative"})
    assert len(output["data"]) == 4  # four headings in CONTENT
    # repeated reads are deterministic
    again = run_tool(repo, {"company": "示例公司", "selection": "top_authoritative"})
    assert [s["heading"] for s in output["data"]] == [s["heading"] for s in again["data"]]


def test_default_registry_degrades_without_repository(monkeypatch):
    registry = build_default_registry()
    execution = asyncio.run(registry.execute(
        "read_document", {"company": "示例公司", "selection": "top_authoritative"}
    ))
    assert execution.output["status"] == "empty"
    assert execution.output["degraded"] is True
    assert "not configured" in execution.output["degraded_reason"]


def test_registry_wiring_with_injected_repository(tmp_path):
    repo = Repository(tmp_path / "read-wired.db")
    repo.initialize()
    _ingest(repo)

    tool = ReadDocumentTool(DocumentRepository(repo))
    registry = build_default_registry(read_document_handler=tool)
    execution = asyncio.run(registry.execute(
        "read_document", {"company": "示例公司", "market": "CN"}
    ))
    assert execution.output["status"] == "ok"
    assert execution.output["data"]
    assert "unconfigured" not in str(execution.output)


def test_read_document_input_schema_rejects_extra_fields():
    registry = build_default_registry()
    with pytest.raises(ToolRegistryError, match="invalid tool input"):
        asyncio.run(registry.execute(
            "read_document", {"company": "示例公司", "evil": "injection"}
        ))


def test_unconfigured_handler_returns_honest_reason():
    output = asyncio.run(read_document_unconfigured(
        ReadDocumentInput.model_validate({"company": "示例公司"})
    ))
    assert output["status"] == "empty"
    assert "document repository is not configured" in output["degraded_reason"]
