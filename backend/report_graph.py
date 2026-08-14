from __future__ import annotations

from dataclasses import dataclass

from backend.evidence import EvidenceBuilder
from backend.reporting import CitationConstrainedReporter
from backend.schemas import ClaimCandidate
from backend.verifier import ClaimVerifier


@dataclass(frozen=True)
class ReportArtifacts:
    evidence: list
    claims: list
    draft: object
    markdown: str
    report_json: dict
    citations: list[dict]


class DeterministicReportGraph:
    def __init__(self) -> None:
        self.evidence_builder = EvidenceBuilder()
        self.verifier = ClaimVerifier()
        self.reporter = CitationConstrainedReporter()

    def build(
        self, *, run_id: str, company: str, question: str,
        evidence_hits: list[dict], claim_candidates: list[ClaimCandidate],
    ) -> ReportArtifacts:
        evidence = self.evidence_builder.build_retrieval_items(run_id, evidence_hits)
        claims = self.verifier.verify(
            claim_candidates, evidence, allowed_access_scopes={"public"}
        )
        draft = self.reporter.build_deterministic(
            company=company, question=question, claims=claims, evidence=evidence,
        )
        markdown, report_json, citations = self.reporter.render(draft, claims, evidence)
        return ReportArtifacts(evidence, claims, draft, markdown, report_json, citations)
