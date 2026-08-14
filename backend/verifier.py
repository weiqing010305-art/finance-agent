from __future__ import annotations

import re

from backend.schemas import ClaimCandidate, EvidenceItem, VerifiedClaim


NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?%?")


def _numbers(value: str) -> set[str]:
    return {item.replace(",", "") for item in NUMBER.findall(value.replace(",", ""))}


class ClaimVerifier:
    def verify(
        self,
        claims: list[ClaimCandidate],
        evidence: list[EvidenceItem],
        *,
        allowed_access_scopes: set[str],
    ) -> list[VerifiedClaim]:
        evidence_by_id = {item.id: item for item in evidence}
        verified: list[VerifiedClaim] = []
        for claim in claims:
            selected = [evidence_by_id[item] for item in claim.evidence_ids if item in evidence_by_id]
            reasons: list[str] = []
            if len(selected) != len(set(claim.evidence_ids)):
                reasons.append("UNKNOWN_EVIDENCE")
            if any(item.access_scope not in allowed_access_scopes for item in selected):
                reasons.append("EVIDENCE_ACCESS_DENIED")
            claim_numbers = _numbers(claim.text)
            evidence_numbers = set().union(*(_numbers(item.excerpt) for item in selected)) if selected else set()
            if claim_numbers and not claim_numbers <= evidence_numbers:
                reasons.append("UNSUPPORTED_NUMBER")
            if claim_numbers and (not claim.period or not claim.unit):
                reasons.append("MISSING_NUMERIC_CONTEXT")
            periods = {item.period for item in selected if item.period}
            if claim.period and periods and claim.period not in periods:
                reasons.append("PERIOD_MISMATCH")
            normalized_claim = " ".join(claim.text.split())
            if selected and not any(
                normalized_claim in " ".join(item.excerpt.split()) for item in selected
            ):
                reasons.append("CLAIM_NOT_EXTRACTIVE")
            excerpts = [item.excerpt for item in selected]
            conflict_markers = any("冲突" in text or "不一致" in text for text in excerpts)
            if conflict_markers:
                status = "conflicted"
                reasons.append("SOURCE_CONFLICT")
            elif reasons:
                status = "unsupported"
            elif not selected:
                status = "unsupported"
                reasons.append("NO_EVIDENCE")
            elif max(item.authority_tier for item in selected) < 2:
                status = "partially_supported"
                reasons.append("LOW_AUTHORITY_SOURCE")
            else:
                status = "supported"
            confidence = {
                "supported": 0.9,
                "partially_supported": 0.55,
                "unsupported": 0.0,
                "conflicted": 0.25,
            }[status]
            verified.append(VerifiedClaim(
                id=claim.id, run_id=claim.run_id, text=claim.text, status=status,
                confidence=confidence, evidence_ids=[item.id for item in selected],
                reason_codes=sorted(set(reasons)), period=claim.period, unit=claim.unit,
                currency=claim.currency,
            ))
        return verified
