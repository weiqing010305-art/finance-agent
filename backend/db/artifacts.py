from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import Engine, and_, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.auth.models import PrincipalContext
from backend.db.durable import DurableConflict
from backend.db.metadata import (
    audit_events_pg, claims_pg, evidence_items_pg, memory_records_pg, reports_pg,
    research_events_pg, research_leases_pg, research_runs_pg,
)
from backend.db.session import principal_transaction
from backend.redaction import redact_text, redact_url, redact_value


def _now() -> datetime: return datetime.now(timezone.utc)
def _json(value) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _hash_text(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
def _hash_json(value) -> str: return _hash_text(_json(value))


class ArtifactVerificationError(RuntimeError): pass


class PostgresResearchArtifacts:
    def __init__(self, engine: Engine, *, retention_days: int = 90):
        self.engine, self.retention_days = engine, retention_days

    def persist_verified_evidence(
        self, principal: PrincipalContext, run_id: str, *, lease_token: str,
        evidence: list[dict], claims: list[dict],
    ) -> None:
        now = _now()
        evidence_by_id: dict[str, dict] = {}
        for item in evidence:
            clean = {
                "id": str(item["id"]), "excerpt": redact_text(str(item["excerpt"])),
                "source_uri": redact_url(str(item["source_uri"])),
                "authority_tier": int(item["authority_tier"]),
            }
            if not clean["excerpt"] or not 0 <= clean["authority_tier"] <= 5:
                raise ArtifactVerificationError("invalid evidence")
            clean["content_hash"] = _hash_json(clean)
            evidence_by_id[clean["id"]] = clean
        if len(evidence_by_id) != len(evidence):
            raise ArtifactVerificationError("duplicate evidence id")
        normalized_claims = []
        for claim in claims:
            ids = sorted(set(map(str, claim.get("evidence_ids", []))))
            if not ids or any(item not in evidence_by_id for item in ids):
                raise ArtifactVerificationError("claim references unknown evidence")
            text = redact_text(str(claim["text"]))
            if claim.get("status") == "supported" and not any(
                text in evidence_by_id[item]["excerpt"] for item in ids
            ):
                raise ArtifactVerificationError("supported claim is not extractive evidence")
            status = str(claim["status"])
            confidence = int(round(float(claim.get("confidence", 0)) * 100))
            if status not in {"supported", "insufficient", "contradicted"} or not 0 <= confidence <= 100:
                raise ArtifactVerificationError("invalid claim decision")
            normalized_claims.append({
                "id": str(claim["id"]), "text": text, "status": str(claim["status"]),
                "confidence": confidence,
                "evidence_ids": ids,
            })
        with principal_transaction(self.engine, principal) as connection:
            if not self._valid_lease(connection, principal, run_id, lease_token, now):
                raise DurableConflict("lease lost")
            run = connection.execute(select(research_runs_pg.c.status).where(and_(
                research_runs_pg.c.id == run_id, research_runs_pg.c.tenant_id == principal.tenant_id,
            ))).scalar_one_or_none()
            if run not in {"running", "pause_requested"}:
                raise DurableConflict("run cannot accept evidence")
            for item in evidence_by_id.values():
                self._insert_if_absent(connection, evidence_items_pg, {
                    "id": item["id"], "run_id": run_id, "tenant_id": principal.tenant_id,
                    "content_hash": item["content_hash"], "excerpt": item["excerpt"],
                    "source_uri": item["source_uri"], "authority_tier": item["authority_tier"],
                    "created_at": now,
                })
                persisted = connection.execute(select(
                    evidence_items_pg.c.run_id, evidence_items_pg.c.tenant_id,
                    evidence_items_pg.c.content_hash, evidence_items_pg.c.excerpt,
                    evidence_items_pg.c.source_uri, evidence_items_pg.c.authority_tier,
                ).where(evidence_items_pg.c.id == item["id"])).mappings().one_or_none()
                if persisted is None or dict(persisted) != {
                    "run_id": run_id, "tenant_id": principal.tenant_id,
                    "content_hash": item["content_hash"], "excerpt": item["excerpt"],
                    "source_uri": item["source_uri"], "authority_tier": item["authority_tier"],
                }:
                    raise ArtifactVerificationError("evidence replay identity conflict")
            for claim in normalized_claims:
                evidence_ids_json = _json(claim["evidence_ids"])
                self._insert_if_absent(connection, claims_pg, {
                    "id": claim["id"], "run_id": run_id, "tenant_id": principal.tenant_id,
                    "claim_text": claim["text"], "status": claim["status"],
                    "confidence": claim["confidence"], "evidence_ids_json": evidence_ids_json,
                    "created_at": now,
                })
                persisted = connection.execute(select(
                    claims_pg.c.run_id, claims_pg.c.tenant_id, claims_pg.c.claim_text,
                    claims_pg.c.status, claims_pg.c.confidence, claims_pg.c.evidence_ids_json,
                ).where(claims_pg.c.id == claim["id"])).mappings().one_or_none()
                if persisted is None or dict(persisted) != {
                    "run_id": run_id, "tenant_id": principal.tenant_id,
                    "claim_text": claim["text"], "status": claim["status"],
                    "confidence": claim["confidence"], "evidence_ids_json": evidence_ids_json,
                }:
                    raise ArtifactVerificationError("claim replay identity conflict")

    @staticmethod
    def _insert_if_absent(connection, table, values: dict) -> None:
        """Make crash recovery idempotent while fencing changed identities."""
        if connection.dialect.name == "postgresql":
            statement = postgresql_insert(table).values(**values).on_conflict_do_nothing(
                index_elements=[table.c.id],
            )
        elif connection.dialect.name == "sqlite":
            statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(
                index_elements=[table.c.id],
            )
        else:
            statement = insert(table).values(**values)
        connection.execute(statement)

    def get_report(self, principal: PrincipalContext, run_id: str) -> dict | None:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(reports_pg).where(and_(
                reports_pg.c.run_id == run_id,
                reports_pg.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
        if row is None:
            return None
        return {
            "markdown": row["markdown"], "report": json.loads(row["report_json"]),
            "citations": json.loads(row["citations_json"]),
            "content_hash": row["content_hash"], "created_at": row["created_at"],
        }

    def complete_report(
        self, principal: PrincipalContext, run_id: str, *, lease_token: str,
        expected_version: int, markdown: str, report: dict, citations: list[dict],
    ) -> dict:
        markdown = redact_text(markdown)
        report = redact_value(report)
        allowed_citation_keys = {"claim_id", "evidence_id", "evidence_hash", "claim_hash"}
        if any(set(item) != allowed_citation_keys for item in citations):
            raise ArtifactVerificationError("citation schema mismatch")
        citations = [redact_value(item) for item in citations]
        normalized = {"markdown": markdown, "report": report, "citations": citations}
        content_hash = _hash_json(normalized)
        if not report.get("complete") or not markdown.strip() or not citations:
            raise ArtifactVerificationError("report must be complete and cited")
        now = _now()
        with principal_transaction(self.engine, principal) as connection:
            existing = connection.execute(select(reports_pg.c.content_hash).where(and_(
                reports_pg.c.run_id == run_id, reports_pg.c.tenant_id == principal.tenant_id,
            ))).scalar_one_or_none()
            if existing is not None:
                if existing != content_hash:
                    raise ArtifactVerificationError("completed report identity conflict")
                return {"run_id": run_id, "content_hash": existing, "replayed": True}
            if not self._valid_lease(connection, principal, run_id, lease_token, now):
                raise DurableConflict("lease lost")
            for index, citation in enumerate(citations, start=1):
                claim = connection.execute(select(
                    claims_pg.c.status, claims_pg.c.claim_text, claims_pg.c.evidence_ids_json,
                ).where(and_(claims_pg.c.id == str(citation.get("claim_id")), claims_pg.c.run_id == run_id,
                             claims_pg.c.tenant_id == principal.tenant_id))).one_or_none()
                evidence = connection.execute(select(evidence_items_pg.c.content_hash).where(and_(
                    evidence_items_pg.c.id == str(citation.get("evidence_id")),
                    evidence_items_pg.c.run_id == run_id,
                    evidence_items_pg.c.tenant_id == principal.tenant_id,
                ))).one_or_none()
                if claim is None or evidence is None or claim.status != "supported":
                    raise ArtifactVerificationError("citation is not supported")
                if str(citation["evidence_id"]) not in json.loads(claim.evidence_ids_json):
                    raise ArtifactVerificationError("citation evidence is not linked to claim")
                if citation.get("evidence_hash") != evidence.content_hash:
                    raise ArtifactVerificationError("citation evidence identity mismatch")
                if citation.get("claim_hash") != _hash_text(claim.claim_text):
                    raise ArtifactVerificationError("citation claim identity mismatch")
                if f"[{index}]" not in markdown:
                    raise ArtifactVerificationError("markdown citation marker missing")
            updated = connection.execute(update(research_runs_pg).where(and_(
                research_runs_pg.c.id == run_id, research_runs_pg.c.tenant_id == principal.tenant_id,
                research_runs_pg.c.status == "running", research_runs_pg.c.state_version == expected_version,
            )).values(status="completed", progress=100, state_version=expected_version + 1, updated_at=now))
            if updated.rowcount != 1:
                raise DurableConflict("completion state conflict")
            connection.execute(insert(reports_pg).values(
                run_id=run_id, tenant_id=principal.tenant_id, markdown=markdown,
                report_json=_json(report), citations_json=_json(citations),
                content_hash=content_hash, created_at=now,
            ))
            connection.execute(delete(research_leases_pg).where(and_(
                research_leases_pg.c.run_id == run_id, research_leases_pg.c.tenant_id == principal.tenant_id,
            )))
            connection.execute(insert(research_events_pg).values(
                id=str(uuid4()), run_id=run_id, tenant_id=principal.tenant_id,
                event_type="run.completed", payload_json=_json({"report_hash": content_hash}), created_at=now,
            ))
            self._audit(connection, principal, "report.completed", "run", run_id, {"report_hash": content_hash})
        return {"run_id": run_id, "content_hash": content_hash, "replayed": False}

    def remember_supported_company_fact(
        self, principal: PrincipalContext, *, run_id: str, claim_id: str, memory_key: str,
    ) -> str:
        now, memory_id = _now(), str(uuid4())
        with principal_transaction(self.engine, principal) as connection:
            claim = connection.execute(select(claims_pg.c.claim_text, claims_pg.c.status).join(
                research_runs_pg, research_runs_pg.c.id == claims_pg.c.run_id,
            ).where(and_(
                claims_pg.c.id == claim_id, claims_pg.c.run_id == run_id,
                claims_pg.c.tenant_id == principal.tenant_id,
                research_runs_pg.c.status == "completed",
            ))).one_or_none()
            if claim is None or claim.status != "supported":
                raise ArtifactVerificationError("memory source is not a completed supported claim")
            content = {"claim": claim.claim_text}
            connection.execute(insert(memory_records_pg).values(
                id=memory_id, tenant_id=principal.tenant_id, user_id=principal.user_id,
                memory_type="company_fact", memory_key=memory_key, status="active",
                content_json=_json(content), content_hash=_hash_json(content),
                source_run_id=run_id, source_claim_id=claim_id,
                expires_at=now + timedelta(days=self.retention_days), created_at=now, updated_at=now,
            ))
            self._audit(connection, principal, "memory.activated", "memory", memory_id, {"source_claim_id": claim_id})
        return memory_id

    @staticmethod
    def _valid_lease(connection, principal, run_id, token, now) -> bool:
        return connection.execute(select(research_leases_pg.c.run_id).where(and_(
            research_leases_pg.c.run_id == run_id, research_leases_pg.c.tenant_id == principal.tenant_id,
            research_leases_pg.c.token_hash == _hash_text(token), research_leases_pg.c.expires_at >= now,
        ))).one_or_none() is not None

    def _audit(self, connection, principal, action, target_type, target_id, payload):
        safe_payload = redact_value(payload)
        now = _now()
        connection.execute(insert(audit_events_pg).values(
            id=str(uuid4()), tenant_id=principal.tenant_id, actor_user_id=principal.user_id,
            action=action, target_type=target_type, target_id=target_id,
            payload_json=_json(safe_payload), payload_hash=_hash_json(safe_payload),
            created_at=now, expires_at=now + timedelta(days=self.retention_days),
        ))


class RetentionMaintenance:
    def __init__(self, engine: Engine): self.engine = engine

    def expire(self, principal: PrincipalContext, *, now: datetime | None = None) -> dict[str, int]:
        current = now or _now()
        with principal_transaction(self.engine, principal) as connection:
            memories = connection.execute(update(memory_records_pg).where(and_(
                memory_records_pg.c.tenant_id == principal.tenant_id,
                memory_records_pg.c.status == "active", memory_records_pg.c.expires_at <= current,
            )).values(status="expired", updated_at=current)).rowcount
            audits = connection.execute(delete(audit_events_pg).where(and_(
                audit_events_pg.c.tenant_id == principal.tenant_id,
                audit_events_pg.c.expires_at <= current,
            ))).rowcount
        return {"memories_expired": memories, "audit_events_deleted": audits}
