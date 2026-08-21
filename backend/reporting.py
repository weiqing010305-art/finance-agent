from __future__ import annotations

import json
import re

from backend.schemas import EvidenceItem, ReportDraft, ReportSection, VerifiedClaim
from backend.verifier import _numbers


class ReportValidationError(ValueError):
    pass


class CitationConstrainedReporter:
    def validate(
        self,
        draft: ReportDraft,
        claims: list[VerifiedClaim],
        evidence: list[EvidenceItem],
    ) -> None:
        claim_by_id = {claim.id: claim for claim in claims}
        evidence_by_id = {item.id: item for item in evidence}
        referenced: set[str] = set()
        for section in draft.sections:
            for claim_id in section.claim_ids:
                claim = claim_by_id.get(claim_id)
                if claim is None:
                    raise ReportValidationError(f"unknown claim id: {claim_id}")
                if claim.status not in {"supported", "partially_supported"}:
                    raise ReportValidationError(f"claim is not reportable: {claim_id}")
                if not claim.evidence_ids or any(item not in evidence_by_id for item in claim.evidence_ids):
                    raise ReportValidationError(f"claim lacks known evidence: {claim_id}")
                referenced.add(claim_id)
                unsupported = _numbers(section.body) - _numbers(claim.text)
                if unsupported:
                    raise ReportValidationError("section contains unsupported numeric values")
        if referenced and not draft.sections:
            raise ReportValidationError("referenced claims require report sections")
        if any(claim_by_id[item].status == "partially_supported" for item in referenced):
            if not draft.limitations:
                raise ReportValidationError("partial claims require limitations disclosure")

    def build_deterministic(
        self,
        *,
        company: str,
        question: str,
        claims: list[VerifiedClaim],
        evidence: list[EvidenceItem],
    ) -> ReportDraft:
        reportable = [
            claim for claim in claims
            if claim.status in {"supported", "partially_supported"} and claim.evidence_ids
        ]
        limitations: list[str] = []
        if not reportable:
            limitations.append("当前没有足够的已验证证据支持实质性结论。")
        if any(claim.status == "partially_supported" for claim in reportable):
            limitations.append("部分结论仅获低权威或不完整证据支持，应谨慎使用。")
        conflicted = [claim for claim in claims if claim.status == "conflicted"]
        if conflicted:
            limitations.append("存在相互冲突的来源，相关结论未纳入确定性正文。")
        sections = [
            ReportSection(
                heading=claim.text.split("=")[0].strip()[:80] or "已验证结论",
                body=claim.text,
                claim_ids=[claim.id],
            )
            for claim in reportable
        ] if reportable else []
        draft = ReportDraft(
            company=company, question=question,
            summary=(
                f"基于 {len(reportable)} 条已验证结论生成。"
                if reportable else "证据不足，无法形成可靠的研究结论。"
            ),
            sections=sections, limitations=limitations,
            degraded=bool(limitations),
        )
        self.validate(draft, claims, evidence)
        return draft

    def render(
        self, draft: ReportDraft, claims: list[VerifiedClaim], evidence: list[EvidenceItem]
    ) -> tuple[str, dict, list[dict]]:
        self.validate(draft, claims, evidence)
        claim_by_id = {claim.id: claim for claim in claims}
        evidence_by_id = {item.id: item for item in evidence}
        citation_numbers: dict[tuple[str, str], int] = {}
        citations: list[dict] = []
        lines = [f"# {draft.company}研究报告", "", draft.summary]
        for section in draft.sections:
            lines.extend(["", f"## {section.heading}", ""])
            for claim_id in section.claim_ids:
                claim = claim_by_id[claim_id]
                markers = []
                for evidence_id in claim.evidence_ids:
                    key = (claim_id, evidence_id)
                    if key not in citation_numbers:
                        number = len(citation_numbers) + 1
                        citation_numbers[key] = number
                        citations.append({
                            "citation_number": number, "claim_id": claim_id,
                            "evidence_id": evidence_id,
                        })
                    markers.append(f"[{citation_numbers[key]}]")
                lines.append(f"- {claim.text} {' '.join(markers)}")
        if draft.limitations:
            lines.extend(["", "## 局限性", ""])
            lines.extend(f"- {item}" for item in draft.limitations)
        if citations:
            lines.extend(["", "## 来源", ""])
            for item in citations:
                ev = evidence_by_id[item["evidence_id"]]
                lines.append(
                    f"[{item['citation_number']}] {ev.title} — {ev.publisher} ({ev.source_uri})"
                )
        report_json = draft.model_dump()
        report_json["citations"] = citations
        return "\n".join(lines) + "\n", report_json, citations
