import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from backend.auth.models import PrincipalContext
from backend.db.artifacts import ArtifactVerificationError, PostgresResearchArtifacts, RetentionMaintenance
from backend.db.durable import PostgresDurableRepository
from backend.db.metadata import (
    audit_events_pg, evidence_items_pg, memberships, memory_records_pg, metadata, tenants, users,
)


def _setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine); now = datetime.now(timezone.utc)
    with engine.begin() as c:
        for uid, tid in (("u1", "t1"), ("u2", "t2")):
            c.execute(users.insert().values(id=uid, email=f"{uid}@example.com", password_hash="x", created_at=now))
            c.execute(tenants.insert().values(id=tid, name=tid, created_at=now))
            c.execute(memberships.insert().values(tenant_id=tid, user_id=uid, role="owner"))
    principal = PrincipalContext("u1", "t1", "owner")
    created = PostgresDurableRepository(engine).create_run(
        principal, company="Tencent", question="cash", idempotency_key="k",
        plan={"steps": ["evidence", "report"]}, owner_id="worker",
    )
    return engine, principal, created


def _persist(artifacts, principal, created):
    artifacts.persist_verified_evidence(
        principal, created.run_id, lease_token=created.lease_token,
        evidence=[{"id": "e1", "excerpt": "经营现金流持续改善。", "source_uri": "https://example.com/r?token=secret", "authority_tier": 4}],
        claims=[{"id": "c1", "text": "经营现金流持续改善。", "status": "supported", "confidence": .9, "evidence_ids": ["e1"]}],
    )


def test_report_completion_requires_persisted_claim_evidence_identity_and_is_atomic():
    engine, principal, created = _setup(); artifacts = PostgresResearchArtifacts(engine)
    _persist(artifacts, principal, created)
    with engine.connect() as c:
        evidence_hash = c.scalar(select(evidence_items_pg.c.content_hash))
    citations = [{
        "claim_id": "c1", "evidence_id": "e1", "evidence_hash": evidence_hash,
        "claim_hash": hashlib.sha256("经营现金流持续改善。".encode()).hexdigest(),
    }]
    result = artifacts.complete_report(
        principal, created.run_id, lease_token=created.lease_token, expected_version=0,
        markdown="结论：改善。[1]", report={"complete": True}, citations=citations,
    )
    assert result["replayed"] is False
    replay = artifacts.complete_report(
        principal, created.run_id, lease_token="already-consumed", expected_version=0,
        markdown="结论：改善。[1]", report={"complete": True}, citations=citations,
    )
    assert replay["replayed"] is True
    memory_id = artifacts.remember_supported_company_fact(
        principal, run_id=created.run_id, claim_id="c1", memory_key="cash-flow",
    )
    with engine.connect() as c:
        assert c.scalar(select(memory_records_pg.c.status).where(memory_records_pg.c.id == memory_id)) == "active"


def test_false_semantic_claim_and_forged_citation_are_rejected():
    engine, principal, created = _setup(); artifacts = PostgresResearchArtifacts(engine)
    with pytest.raises(ArtifactVerificationError, match="not extractive"):
        artifacts.persist_verified_evidence(
            principal, created.run_id, lease_token=created.lease_token,
            evidence=[{"id": "e1", "excerpt": "现金流改善", "source_uri": "https://example.com", "authority_tier": 3}],
            claims=[{"id": "c1", "text": "公司即将破产", "status": "supported", "confidence": .9, "evidence_ids": ["e1"]}],
        )
    _persist(artifacts, principal, created)
    with pytest.raises(ArtifactVerificationError, match="identity"):
        artifacts.complete_report(
            principal, created.run_id, lease_token=created.lease_token, expected_version=0,
            markdown="报告[1]", report={"complete": True}, citations=[{
                "claim_id": "c1", "evidence_id": "e1", "evidence_hash": "forged", "claim_hash": "forged",
            }],
        )
    assert PostgresDurableRepository(engine).get_run(principal, created.run_id)["status"] == "running"


def test_evidence_and_claim_replay_is_idempotent_but_changed_identity_fails_closed():
    engine, principal, created = _setup(); artifacts = PostgresResearchArtifacts(engine)
    _persist(artifacts, principal, created)
    _persist(artifacts, principal, created)
    with engine.connect() as connection:
        assert connection.scalar(select(evidence_items_pg.c.id).where(evidence_items_pg.c.id == "e1")) == "e1"
    with pytest.raises(ArtifactVerificationError, match="evidence replay identity conflict"):
        artifacts.persist_verified_evidence(
            principal, created.run_id, lease_token=created.lease_token,
            evidence=[{"id": "e1", "excerpt": "被篡改的证据", "source_uri": "https://example.com/r", "authority_tier": 4}],
            claims=[{"id": "c2", "text": "被篡改的证据", "status": "supported", "confidence": .9, "evidence_ids": ["e1"]}],
        )


def test_90_day_retention_expires_memory_and_deletes_audit_per_tenant():
    engine, principal, created = _setup(); artifacts = PostgresResearchArtifacts(engine)
    _persist(artifacts, principal, created)
    with engine.connect() as c: evidence_hash = c.scalar(select(evidence_items_pg.c.content_hash))
    artifacts.complete_report(principal, created.run_id, lease_token=created.lease_token, expected_version=0,
        markdown="报告[1]", report={"complete": True}, citations=[{"claim_id":"c1","evidence_id":"e1","evidence_hash":evidence_hash,"claim_hash":hashlib.sha256("经营现金流持续改善。".encode()).hexdigest()}])
    memory_id = artifacts.remember_supported_company_fact(principal, run_id=created.run_id, claim_id="c1", memory_key="k")
    past = datetime.now(timezone.utc) - timedelta(days=1)
    with engine.begin() as c:
        c.execute(update(memory_records_pg).values(expires_at=past))
        c.execute(update(audit_events_pg).values(expires_at=past))
    result = RetentionMaintenance(engine).expire(principal)
    assert result["memories_expired"] == 1 and result["audit_events_deleted"] == 2
