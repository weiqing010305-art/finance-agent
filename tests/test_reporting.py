from __future__ import annotations

import pytest

from backend.evidence import EvidenceBuilder
from backend.reporting import CitationConstrainedReporter, ReportValidationError
from backend.schemas import ReportDraft, ReportSection, VerifiedClaim


def _fixture(status="supported"):
    evidence = EvidenceBuilder().build_retrieval_items("r1", [{
        "text": "2024年收入增长10%。", "source_uri": "https://example.com/a",
        "title": "年报", "publisher": "交易所", "authority_tier": 5,
    }])[0]
    claim = VerifiedClaim(
        id="cl1", run_id="r1", text="2024年收入增长10%", status=status,
        confidence=0.9, evidence_ids=[evidence.id], period="2024", unit="%",
        reason_codes=[],
    )
    return evidence, claim


def test_deterministic_report_has_stable_claim_to_evidence_citations():
    evidence, claim = _fixture()
    reporter = CitationConstrainedReporter()
    draft = reporter.build_deterministic(
        company="示例公司", question="分析收入", claims=[claim], evidence=[evidence]
    )
    markdown, report_json, citations = reporter.render(draft, [claim], [evidence])
    assert "收入增长10% [1]" in markdown
    assert citations == [{"citation_number": 1, "claim_id": "cl1", "evidence_id": evidence.id}]
    assert report_json["citations"] == citations


def test_unknown_or_unsupported_claim_and_invented_number_are_rejected():
    evidence, claim = _fixture()
    reporter = CitationConstrainedReporter()
    for draft in (
        ReportDraft(company="公司", question="问题", summary="摘要", sections=[
            ReportSection(heading="结论", body="结论", claim_ids=["invented"])
        ]),
        ReportDraft(company="公司", question="问题", summary="摘要", sections=[
            ReportSection(heading="结论", body="2024年增长99%", claim_ids=["cl1"])
        ]),
    ):
        with pytest.raises(ReportValidationError):
            reporter.validate(draft, [claim], [evidence])
    unsupported = claim.model_copy(update={"status": "unsupported"})
    with pytest.raises(ReportValidationError):
        reporter.validate(ReportDraft(
            company="公司", question="问题", summary="摘要",
            sections=[ReportSection(heading="结论", body=unsupported.text, claim_ids=["cl1"])],
        ), [unsupported], [evidence])


def test_zero_evidence_and_conflicts_produce_explicit_limitations():
    reporter = CitationConstrainedReporter()
    draft = reporter.build_deterministic(
        company="公司", question="问题", claims=[], evidence=[]
    )
    assert draft.degraded and "证据不足" in draft.summary
    _, conflicted = _fixture(status="conflicted")
    conflict_draft = reporter.build_deterministic(
        company="公司", question="问题", claims=[conflicted], evidence=[]
    )
    assert any("冲突" in item for item in conflict_draft.limitations)
