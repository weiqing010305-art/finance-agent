from __future__ import annotations

import sqlite3

import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner
from backend.evidence import EvidenceBuilder
from backend.memory import MemoryService
from backend.schemas import MemoryCandidate, MemoryScope, ResearchCreate, VerifiedClaim


def _company_runtime(tmp_path):
    repo = Repository(tmp_path / "memory-life.db")
    repo.initialize()
    runner = DurableRunner(repo)
    created = runner.create_run(
        ResearchCreate(
            company="Tencent", symbol="0700.HK", market="HK",
            question="Analyze revenue growth",
        ),
        owner_id="memory-test", idempotency_key="memory-source-run",
    )
    evidence = EvidenceBuilder().build_retrieval_items(created.run["id"], [{
        "text": "Tencent 2024 revenue increased 8%.",
        "source_uri": "https://example.com/filing-2024",
        "title": "2024 filing", "publisher": "HKEX", "authority_tier": 5,
        "company": "Tencent", "period": "2024",
    }])[0]
    claim = VerifiedClaim(
        id="claim-revenue-2024", run_id=created.run["id"],
        text="Tencent 2024 revenue increased 8%.", status="supported",
        confidence=.9, evidence_ids=[evidence.id], reason_codes=[], period="2024",
        unit="%",
    )
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    return repo, created, evidence, claim


def _company_candidate(created, evidence, claim, *, key="fact-1", text=None):
    content_text = text or claim.text
    return MemoryCandidate(
        memory_type="company_fact", memory_key="revenue_growth", period=claim.period,
        scope=MemoryScope(
            scope_kind="public_company", tenant_id="public", company="Tencent",
            symbol="0700.HK", market="HK",
        ),
        content={"metric": "revenue_growth", "value": 8, "period": claim.period},
        content_text=content_text, idempotency_key=key, confidence=claim.confidence,
        source_run_id=created.run["id"], claim_ids=[claim.id], evidence_ids=[evidence.id],
    )


def test_company_fact_reverifies_persisted_claim_and_evidence(tmp_path):
    repo, created, evidence, claim = _company_runtime(tmp_path)
    view = MemoryService(repo).remember(_company_candidate(created, evidence, claim))
    assert view.status == "active"
    assert view.evidence_ids == [evidence.id]
    assert view.claim_ids == [claim.id]
    assert view.expires_at is not None
    with repo.connect() as connection:
        assert [row[0] for row in connection.execute(
            "SELECT kind FROM memory_events WHERE memory_id=? ORDER BY id", (view.memory_id,)
        )] == ["memory.candidate_created", "memory.verified", "memory.active"]


def test_company_fact_rejects_unknown_or_non_extractive_source(tmp_path):
    repo, created, evidence, claim = _company_runtime(tmp_path)
    service = MemoryService(repo)
    with pytest.raises(ValueError, match="extractive supported claim"):
        service.remember(_company_candidate(
            created, evidence, claim, key="invented", text="Revenue doubled without evidence."
        ))
    changed = _company_candidate(created, evidence, claim, key="unknown").model_copy(
        update={"claim_ids": ["missing"]}
    )
    with pytest.raises(ValueError, match="unknown claim"):
        service.remember(changed)
    wrong_company = _company_candidate(created, evidence, claim, key="wrong-company").model_copy(
        update={"scope": MemoryScope(
            scope_kind="public_company", tenant_id="public", company="Alibaba",
            symbol="9988.HK", market="HK",
        )}
    )
    with pytest.raises(ValueError, match="source run entity"):
        service.remember(wrong_company)


def test_user_preference_requires_confirmation_and_has_no_expiry(tmp_path):
    repo = Repository(tmp_path / "preference.db")
    repo.initialize()
    scope = MemoryScope(scope_kind="user", tenant_id="local", user_id="default")
    with pytest.raises(ValueError, match="explicit user confirmation"):
        MemoryCandidate(
            memory_type="user_preference", memory_key="report_style", scope=scope,
            content={"style": "concise"}, content_text="Prefer concise reports",
            idempotency_key="pref-no", confidence=1,
        )
    view = MemoryService(repo).remember(MemoryCandidate(
        memory_type="user_preference", memory_key="report_style", scope=scope,
        content={"style": "concise"}, content_text="Prefer concise reports",
        idempotency_key="pref-yes", confidence=1, explicit_user_confirmation=True,
    ))
    assert view.status == "active" and view.expires_at is None


def test_idempotency_replays_identity_and_rejects_changed_request(tmp_path):
    repo, created, evidence, claim = _company_runtime(tmp_path)
    service = MemoryService(repo)
    candidate = _company_candidate(created, evidence, claim, key="stable-request")
    first = service.remember(candidate)
    assert service.remember(candidate).id == first.id
    with pytest.raises(ValueError, match="different identity"):
        service.remember(candidate.model_copy(update={"confidence": .8}))


def test_database_rejects_illegal_state_and_content_mutation(tmp_path):
    repo, created, evidence, claim = _company_runtime(tmp_path)
    view = MemoryService(repo).remember(_company_candidate(created, evidence, claim))
    with repo.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="illegal memory version transition"):
            connection.execute("UPDATE memory_versions SET status='candidate' WHERE id=?", (view.id,))
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute("UPDATE memory_versions SET content_text='tampered' WHERE id=?", (view.id,))
