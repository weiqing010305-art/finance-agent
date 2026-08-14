from __future__ import annotations

from backend.evidence import EvidenceBuilder
from backend.schemas import ClaimCandidate
from backend.verifier import ClaimVerifier


def _evidence(run_id="r1", excerpt="2024年收入增长10%，单位为人民币亿元。", authority=5):
    return EvidenceBuilder().build_retrieval_items(run_id, [{
        "chunk_id": "c1", "document_version_id": "v1", "text": excerpt,
        "source_uri": "https://example.com/report?token=secret", "title": "年报",
        "publisher": "交易所", "source_type": "filing", "authority_tier": authority,
        "period": "2024", "page": 10,
    }])[0]


def test_supported_numeric_claim_requires_period_unit_and_exact_evidence_number():
    evidence = _evidence()
    claim = ClaimCandidate(
        id="cl1", run_id="r1", text="2024年收入增长10%",
        evidence_ids=[evidence.id], period="2024", unit="%", currency="CNY",
    )
    result = ClaimVerifier().verify([claim], [evidence], allowed_access_scopes={"public"})[0]
    assert result.status == "supported"
    assert "token=secret" not in evidence.source_uri


def test_unsupported_number_unknown_or_inaccessible_evidence_fail_closed():
    evidence = _evidence()
    claims = [
        ClaimCandidate(id="a", run_id="r1", text="收入增长12%", evidence_ids=[evidence.id], period="2024", unit="%"),
        ClaimCandidate(id="b", run_id="r1", text="收入增长10%", evidence_ids=["missing"], period="2024", unit="%"),
        ClaimCandidate(id="c", run_id="r1", text="收入增长10%", evidence_ids=[evidence.id], period="2024", unit="%"),
    ]
    results = ClaimVerifier().verify(claims, [evidence], allowed_access_scopes={"private:user"})
    assert [item.status for item in results] == ["unsupported", "unsupported", "unsupported"]
    assert "UNSUPPORTED_NUMBER" in results[0].reason_codes
    assert "UNKNOWN_EVIDENCE" in results[1].reason_codes
    assert "EVIDENCE_ACCESS_DENIED" in results[2].reason_codes


def test_conflict_and_low_authority_are_disclosed():
    conflict = _evidence(excerpt="两份来源数值冲突，2024年为10%。")
    weak = _evidence(excerpt="现金流改善。", authority=1)
    claims = [
        ClaimCandidate(id="c1", run_id="r1", text="2024年为10%", evidence_ids=[conflict.id], period="2024", unit="%"),
        ClaimCandidate(id="c2", run_id="r1", text="现金流改善", evidence_ids=[weak.id]),
    ]
    results = ClaimVerifier().verify(claims, [conflict, weak], allowed_access_scopes={"public"})
    assert results[0].status == "conflicted"
    assert results[1].status == "partially_supported"


def test_unrelated_claim_cannot_be_supported_by_authoritative_evidence():
    evidence = _evidence(excerpt="经营现金流持续改善。", authority=5)
    claim = ClaimCandidate(
        id="bankruptcy", run_id="r1", text="公司已经资不抵债并即将破产。",
        evidence_ids=[evidence.id],
    )
    result = ClaimVerifier().verify([claim], [evidence], allowed_access_scopes={"public"})[0]
    assert result.status == "unsupported"
    assert "CLAIM_NOT_EXTRACTIVE" in result.reason_codes
