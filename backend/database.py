from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from backend.migrations import migrate
from backend.redaction import redact_text, redact_url, redact_value
from backend.schemas import (
    DocumentChunk, DocumentSource, EvidenceItem, MemoryCandidate, MemoryScope,
    MemoryView, ResearchCreate, ResearchPlan, VerifiedClaim,
)


from backend.run_states import (
    RUN_STATES,
    RUN_STATE_TRANSITIONS,
    TERMINAL_RUN_STATES,
)


TERMINAL_STATUSES = TERMINAL_RUN_STATES
SIX_RUN_STATES = RUN_STATES
LEGAL_TRANSITIONS = RUN_STATE_TRANSITIONS
REGISTERED_RESEARCH_TOOLS = {
    "search_filings", "search_web", "retrieve_documents", "read_document",
    "extract_financial_facts", "calculate_financial_metrics", "get_quote",
}


def _validate_persisted_plan(plan: dict[str, Any]) -> ResearchPlan:
    validated = ResearchPlan.model_validate(plan)
    unknown = {step.tool_name for step in validated.steps} - REGISTERED_RESEARCH_TOOLS
    if unknown:
        raise ValueError(f"plan references unregistered tools: {sorted(unknown)}")
    return validated


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("datetime must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_public_url(value: str) -> str:
    return redact_url(value)


def _commit_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            migrate(connection)

    @staticmethod
    def _memory_view(connection: sqlite3.Connection, version_id: str) -> MemoryView:
        row = connection.execute(
            """
            SELECT v.*,r.memory_key,r.memory_type,r.scope_kind,r.tenant_id,r.user_id,
                   r.case_id,r.company,r.symbol,r.market
            FROM memory_versions v JOIN memory_records r ON r.id=v.memory_id
            WHERE v.id=?
            """,
            (version_id,),
        ).fetchone()
        if row is None:
            raise KeyError(version_id)
        links = connection.execute(
            "SELECT evidence_id,claim_id FROM memory_evidence WHERE memory_version_id=?",
            (version_id,),
        ).fetchall()
        return MemoryView(
            id=row["id"], memory_id=row["memory_id"], version=row["version"],
            memory_type=row["memory_type"], memory_key=row["memory_key"],
            status=row["status"],
            scope=MemoryScope(
                scope_kind=row["scope_kind"], tenant_id=row["tenant_id"],
                user_id=row["user_id"], case_id=row["case_id"], company=row["company"],
                symbol=row["symbol"], market=row["market"],
            ),
            content=json.loads(row["content_json"]), content_text=row["content_text"],
            confidence=row["confidence"], period=row["period"],
            evidence_ids=sorted({item["evidence_id"] for item in links}),
            claim_ids=sorted({item["claim_id"] for item in links}),
            expires_at=row["expires_at"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _add_memory_event(
        connection: sqlite3.Connection, *, memory_id: str, version_id: str | None,
        kind: str, reason_code: str, payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_events(
                memory_id,memory_version_id,kind,reason_code,payload_json,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (memory_id, version_id, kind, reason_code, _json(payload or {}), utc_now()),
        )

    @staticmethod
    def _validate_memory_source(
        connection: sqlite3.Connection, candidate: MemoryCandidate
    ) -> tuple[list[tuple[str, str]], str, str]:
        if candidate.memory_type == "company_fact":
            source_run = connection.execute(
                "SELECT company,symbol,market FROM agent_runs WHERE id=?",
                (candidate.source_run_id,),
            ).fetchone()
            if source_run is None or (
                source_run["company"] != candidate.scope.company
                or (source_run["symbol"] or None) != (candidate.scope.symbol or None)
                or source_run["market"] != candidate.scope.market
            ):
                raise ValueError("company fact scope does not match its source run entity")
            placeholders = ",".join("?" for _ in candidate.claim_ids)
            claims = connection.execute(
                f"SELECT * FROM claims WHERE id IN ({placeholders})",
                candidate.claim_ids,
            ).fetchall()
            if len(claims) != len(set(candidate.claim_ids)):
                raise ValueError("memory references an unknown claim")
            if any(
                row["run_id"] != candidate.source_run_id or row["status"] != "supported"
                for row in claims
            ):
                raise ValueError("company fact requires supported claims from its source run")
            matching_claims = [row for row in claims if row["text"] == candidate.content_text]
            if len(matching_claims) != 1:
                raise ValueError("company fact must be an extractive supported claim")
            source_claim = matching_claims[0]
            if (
                candidate.period != source_claim["period"]
                or candidate.confidence != float(source_claim["confidence"])
            ):
                raise ValueError("company fact period and confidence must match its claim")
            links = connection.execute(
                f"""
                SELECT ce.claim_id,ce.evidence_id,e.run_id,e.access_scope,e.company
                FROM claim_evidence ce JOIN evidence_items e ON e.id=ce.evidence_id
                WHERE ce.claim_id IN ({placeholders}) AND ce.relation='supports'
                """,
                candidate.claim_ids,
            ).fetchall()
            allowed = {
                (row["claim_id"], row["evidence_id"])
                for row in links
                if row["run_id"] == candidate.source_run_id
                and row["access_scope"] == "public"
                and (not row["company"] or row["company"] == candidate.scope.company)
            }
            requested_evidence = set(candidate.evidence_ids)
            if not requested_evidence or requested_evidence != {item[1] for item in allowed}:
                raise ValueError("company fact evidence does not match supported public links")
            from backend.schemas import ClaimCandidate, EvidenceItem
            from backend.verifier import ClaimVerifier
            evidence_rows = connection.execute(
                f"SELECT * FROM evidence_items WHERE id IN ({','.join('?' for _ in candidate.evidence_ids)})",
                candidate.evidence_ids,
            ).fetchall()
            evidence_models = [EvidenceItem(
                id=row["id"], run_id=row["run_id"], document_version_id=row["document_version_id"],
                chunk_id=row["chunk_id"], source_uri=row["source_uri"], title=row["title"],
                publisher=row["publisher"], source_type=row["source_type"], excerpt=row["excerpt"],
                content_sha256=row["content_sha256"], access_scope=row["access_scope"],
                authority_tier=row["authority_tier"], published_at=row["published_at"],
                retrieved_at=row["retrieved_at"], page=row["page"], section=row["section"],
                company=row["company"], period=row["period"],
            ) for row in evidence_rows]
            reverified = ClaimVerifier().verify([ClaimCandidate(
                id=source_claim["id"], run_id=source_claim["run_id"], text=source_claim["text"],
                evidence_ids=candidate.evidence_ids, period=source_claim["period"],
                unit=source_claim["unit"], currency=source_claim["currency"],
            )], evidence_models, allowed_access_scopes={"public"})[0]
            if (
                reverified.status != "supported"
                or reverified.confidence != float(source_claim["confidence"])
                or reverified.reason_codes != json.loads(source_claim["reason_codes_json"])
            ):
                raise ValueError("persisted claim no longer passes deterministic verification")
            source_fingerprint = _commit_fingerprint({
                "claim": dict(source_claim),
                "evidence": [dict(row) for row in evidence_rows],
            })
            return sorted(allowed), "verified_evidence", source_fingerprint
        if candidate.memory_type == "entity_identity":
            row = connection.execute(
                "SELECT company,symbol,market FROM cases WHERE id=?", (candidate.scope.case_id,)
            ).fetchone()
            if row is None:
                raise ValueError("entity identity requires a persisted case")
            if (
                row["company"] != candidate.scope.company
                or row["symbol"] != candidate.scope.symbol
                or row["market"] != candidate.scope.market
            ):
                raise ValueError("entity identity does not match the persisted case")
            authorization_kind = "explicit_user_confirmation"
            source_fingerprint = _commit_fingerprint(candidate.model_dump(mode="json"))
        elif candidate.memory_type == "case_summary":
            row = connection.execute(
                "SELECT case_id,summary FROM case_summaries WHERE id=?", (candidate.source_summary_id,)
            ).fetchone()
            if row is None or row["case_id"] != candidate.scope.case_id:
                raise ValueError("case summary cursor does not match its scope")
            if redact_text(row["summary"]) != redact_text(candidate.content_text):
                raise ValueError("case summary content must match the persisted summary")
            authorization_kind = "persisted_summary"
            source_fingerprint = _commit_fingerprint(dict(row))
        elif candidate.memory_type == "task_experience":
            raise ValueError(
                "task experience writes are disabled until a persisted execution-summary contract exists"
            )
        else:
            authorization_kind = "explicit_user_confirmation"
            source_fingerprint = _commit_fingerprint(candidate.model_dump(mode="json"))
        return [], authorization_kind, source_fingerprint

    def persist_memory_candidate_atomic(
        self, candidate: MemoryCandidate, *, scope_digest: str, content_digest: str,
        fingerprint: str, expires_at: str | None, conflict_expires_at: str,
    ) -> MemoryView:
        now = utc_now()
        if expires_at is not None:
            expires_at = _canonical_utc(expires_at)
        content_json = _json(redact_value(candidate.content))
        content_text = redact_text(candidate.content_text)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT memory_version_id,request_fingerprint FROM memory_write_requests WHERE idempotency_key=?",
                (candidate.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["request_fingerprint"] != fingerprint:
                    raise ValueError("memory idempotency key was reused with different identity")
                return self._memory_view(connection, replay["memory_version_id"])
            links, authorization_kind, source_fingerprint = self._validate_memory_source(
                connection, candidate
            )
            record = connection.execute(
                "SELECT * FROM memory_records WHERE scope_hash=? AND memory_key=?",
                (scope_digest, candidate.memory_key),
            ).fetchone()
            if record is not None and (
                record["memory_type"] != candidate.memory_type
                or record["scope_kind"] != candidate.scope.scope_kind
                or bool(record["tombstoned"])
            ):
                raise ValueError("memory key conflicts with an existing record identity")
            if record is None:
                memory_id = "mem_" + hashlib.sha256(
                    f"{scope_digest}:{candidate.memory_key}".encode("utf-8")
                ).hexdigest()[:32]
                connection.execute(
                    """
                    INSERT INTO memory_records(
                        id,memory_key,memory_type,scope_kind,scope_hash,tenant_id,user_id,
                        case_id,company,symbol,market,tombstoned,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                    """,
                    (
                        memory_id, candidate.memory_key, candidate.memory_type,
                        candidate.scope.scope_kind, scope_digest, candidate.scope.tenant_id,
                        candidate.scope.user_id, candidate.scope.case_id, candidate.scope.company,
                        candidate.scope.symbol, candidate.scope.market, now, now,
                    ),
                )
                active = None
            else:
                memory_id = record["id"]
                active = connection.execute(
                    "SELECT * FROM memory_versions WHERE memory_id=? AND status='active'",
                    (memory_id,),
                ).fetchone()
            unresolved = connection.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? AND status='conflicted' ORDER BY version",
                (memory_id,),
            ).fetchall()
            if active is not None and active["content_sha256"] == content_digest:
                for claim_id, evidence_id in links:
                    connection.execute(
                        """
                        INSERT INTO memory_evidence(memory_version_id,evidence_id,claim_id,created_at)
                        VALUES (?,?,?,?) ON CONFLICT DO NOTHING
                        """,
                        (active["id"], evidence_id, claim_id, now),
                    )
                connection.execute(
                    "UPDATE memory_versions SET expires_at=?,updated_at=? WHERE id=?",
                    (expires_at, now, active["id"]),
                )
                self._add_memory_event(
                    connection, memory_id=memory_id, version_id=active["id"],
                    kind="memory.merged", reason_code="same_content_evidence_merge",
                    payload={"content_sha256": content_digest},
                )
                connection.execute(
                    "INSERT INTO memory_write_requests(idempotency_key,request_fingerprint,memory_version_id,created_at) VALUES (?,?,?,?)",
                    (candidate.idempotency_key, fingerprint, active["id"], now),
                )
                return self._memory_view(connection, active["id"])
            next_version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM memory_versions WHERE memory_id=?",
                (memory_id,),
            ).fetchone()[0])
            version_id = str(uuid4())
            supersede = active is not None and (
                candidate.memory_type in {"user_preference", "entity_identity"}
                or (candidate.period is not None and active["period"] is not None
                    and candidate.period > active["period"])
                or candidate.confidence > float(active["confidence"])
            )
            resolves_unresolved = bool(unresolved) and all(
                (candidate.period is not None and item["period"] is not None
                 and candidate.period > item["period"])
                or candidate.confidence > float(item["confidence"])
                for item in unresolved
            )
            final_status = "active" if (
                (active is None and not unresolved) or supersede or resolves_unresolved
            ) else "conflicted"
            effective_expiry = conflict_expires_at if final_status == "conflicted" else expires_at
            connection.execute(
                """
                INSERT INTO memory_versions(
                    id,memory_id,version,status,content_json,content_text,content_sha256,
                    request_fingerprint,idempotency_key,confidence,period,source_run_id,
                    source_summary_id,supersedes_version_id,expires_at,created_at,updated_at
                ) VALUES (?,?,?,'candidate',?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    version_id, memory_id, next_version, content_json, content_text,
                    content_digest, fingerprint, candidate.idempotency_key,
                    candidate.confidence, candidate.period, candidate.source_run_id,
                    candidate.source_summary_id, active["id"] if supersede else None,
                    effective_expiry, now, now,
                ),
            )
            self._add_memory_event(
                connection, memory_id=memory_id, version_id=version_id,
                kind="memory.candidate_created", reason_code="write_policy_passed",
            )
            connection.execute(
                "UPDATE memory_versions SET status='verified',updated_at=? WHERE id=?",
                (now, version_id),
            )
            self._add_memory_event(
                connection, memory_id=memory_id, version_id=version_id,
                kind="memory.verified", reason_code="deterministic_source_reverified",
            )
            for claim_id, evidence_id in links:
                connection.execute(
                    "INSERT INTO memory_evidence(memory_version_id,evidence_id,claim_id,created_at) VALUES (?,?,?,?)",
                    (version_id, evidence_id, claim_id, now),
                )
            connection.execute(
                "INSERT INTO memory_activation_authorizations(memory_version_id,authorization_kind,source_fingerprint,created_at) VALUES (?,?,?,?)",
                (version_id, authorization_kind, source_fingerprint, now),
            )
            if active is not None:
                if supersede:
                    connection.execute(
                        "UPDATE memory_versions SET status='superseded',updated_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE memory_versions SET status='conflicted',expires_at=?,updated_at=? WHERE id=?",
                        (conflict_expires_at, now, active["id"]),
                    )
            if resolves_unresolved:
                connection.execute(
                    "UPDATE memory_versions SET status='superseded',updated_at=? WHERE memory_id=? AND status='conflicted' AND id!=?",
                    (now, memory_id, version_id),
                )
            connection.execute(
                "UPDATE memory_versions SET status=?,updated_at=? WHERE id=?",
                (final_status, now, version_id),
            )
            connection.execute(
                "INSERT INTO memory_write_requests(idempotency_key,request_fingerprint,memory_version_id,created_at) VALUES (?,?,?,?)",
                (candidate.idempotency_key, fingerprint, version_id, now),
            )
            connection.execute(
                "UPDATE memory_records SET updated_at=? WHERE id=?", (now, memory_id)
            )
            self._add_memory_event(
                connection, memory_id=memory_id, version_id=version_id,
                kind=f"memory.{final_status}",
                reason_code="newer_or_stronger" if supersede else (
                    "first_verified_version" if active is None else "conflicting_value"
                ),
            )
            return self._memory_view(connection, version_id)

    def list_memory_versions(self, memory_id: str) -> list[MemoryView]:
        with self.connect() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT id FROM memory_versions WHERE memory_id=? ORDER BY version",
                (memory_id,),
            ).fetchall()]
            return [self._memory_view(connection, item) for item in ids]

    def expire_memory_versions(self, *, now: str | None = None) -> int:
        boundary = _canonical_utc(now) if now is not None else utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id,memory_id FROM memory_versions
                WHERE status IN ('active','candidate','conflicted')
                  AND expires_at IS NOT NULL AND expires_at<=?
                """,
                (boundary,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE memory_versions SET status='expired',updated_at=? WHERE id=? AND status IN ('active','candidate','conflicted')",
                    (boundary, row["id"]),
                )
                self._add_memory_event(
                    connection, memory_id=row["memory_id"], version_id=row["id"],
                    kind="memory.expired", reason_code="ttl_elapsed",
                    payload={"expired_at": boundary},
                )
            return len(rows)

    def query_active_memories(
        self, *, scope_hashes: list[str], memory_types: set[str] | None = None,
        now: str | None = None, limit: int = 32,
    ) -> list[MemoryView]:
        if not scope_hashes:
            return []
        boundary = _canonical_utc(now) if now is not None else utc_now()
        placeholders = ",".join("?" for _ in scope_hashes)
        type_clause = ""
        params: list[Any] = [*scope_hashes, boundary]
        if memory_types:
            type_placeholders = ",".join("?" for _ in memory_types)
            type_clause = f" AND r.memory_type IN ({type_placeholders})"
            params.extend(sorted(memory_types))
        params.append(max(1, min(limit, 100)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT v.id FROM memory_versions v
                JOIN memory_records r ON r.id=v.memory_id
                WHERE r.scope_hash IN ({placeholders}) AND r.tombstoned=0
                  AND v.status='active' AND (v.expires_at IS NULL OR v.expires_at>?)
                  {type_clause}
                ORDER BY v.confidence DESC,v.updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._memory_view(connection, row["id"]) for row in rows]

    def tombstone_memory_atomic(
        self, memory_id: str, *, tenant_id: str, user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = utc_now()
        scope_digest = hashlib.sha256(
            _json({"tenant_id": tenant_id, "user_id": user_id}).encode("utf-8")
        ).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_job = connection.execute(
                "SELECT * FROM memory_deletion_jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing_job is not None:
                if existing_job["memory_id"] != memory_id:
                    raise ValueError("deletion idempotency key was reused for another memory")
                return dict(existing_job)
            record = connection.execute(
                "SELECT * FROM memory_records WHERE id=?", (memory_id,)
            ).fetchone()
            if record is None:
                raise KeyError(memory_id)
            if record["scope_kind"] not in {"user", "case"}:
                raise PermissionError("shared public memory cannot be globally deleted by a user")
            if record["tenant_id"] != tenant_id or record["user_id"] != user_id:
                raise PermissionError("memory belongs to another principal")
            connection.execute(
                "UPDATE memory_records SET tombstoned=1,updated_at=? WHERE id=?", (now, memory_id)
            )
            connection.execute(
                "UPDATE memory_versions SET status='deleted',updated_at=? WHERE memory_id=? AND status!='deleted'",
                (now, memory_id),
            )
            self._add_memory_event(
                connection, memory_id=memory_id, version_id=None,
                kind="memory.tombstoned", reason_code="user_delete_requested",
                payload={"content_hash_only": True},
            )
            job_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO memory_deletion_jobs(
                    id,memory_id,scope_hash,status,idempotency_key,attempt,created_at,updated_at
                ) VALUES (?,?,?,'pending',?,0,?,?)
                """,
                (job_id, memory_id, scope_digest, idempotency_key, now, now),
            )
            return dict(connection.execute(
                "SELECT * FROM memory_deletion_jobs WHERE id=?", (job_id,)
            ).fetchone())

    def claim_memory_deletion_job(self, job_id: str, *, ttl_seconds: int = 300) -> str:
        from datetime import timedelta
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        token = str(uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE memory_deletion_jobs SET status='claimed',claim_token_hash=?,
                    claim_expires_at=?,attempt=attempt+1,error=NULL,updated_at=?
                WHERE id=? AND (
                    status IN ('pending','failed') OR
                    (status='claimed' AND claim_expires_at<=?)
                )
                """,
                (_token_hash(token), expires, now, job_id, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("deletion job is not claimable")
        return token

    def finish_memory_deletion_job(self, job_id: str, *, claim_token: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM memory_deletion_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                row is None or row["status"] != "claimed"
                or row["claim_token_hash"] != _token_hash(claim_token)
                or row["claim_expires_at"] <= now
            ):
                raise ValueError("deletion job claim was lost")
            # SQLite is authoritative. Removing versions cascades private evidence
            # links and idempotency rows while the tombstoned record and body-free
            # memory events remain as the audit trail. Derived indexes are keyed by
            # these exact version ids and must be removed before this completion edge.
            connection.execute(
                "DELETE FROM memory_versions WHERE memory_id=?", (row["memory_id"],)
            )
            connection.execute(
                "UPDATE memory_deletion_jobs SET status='completed',claim_token_hash=NULL,claim_expires_at=NULL,updated_at=? WHERE id=?",
                (now, job_id),
            )
            return dict(connection.execute(
                "SELECT * FROM memory_deletion_jobs WHERE id=?", (job_id,)
            ).fetchone())

    def get_memory_deletion_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_deletion_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_memory_deletion_job_for_principal(
        self, job_id: str, *, tenant_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT j.* FROM memory_deletion_jobs j
                JOIN memory_records r ON r.id=j.memory_id
                WHERE j.id=? AND r.tenant_id=? AND r.user_id=?
                  AND r.scope_kind IN ('user','case')
                """,
                (job_id, tenant_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def tombstone_all_private_memories_atomic(
        self, *, tenant_id: str, user_id: str, idempotency_prefix: str,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            ids = [row[0] for row in connection.execute(
                """
                SELECT id FROM memory_records
                WHERE tenant_id=? AND user_id=? AND scope_kind IN ('user','case')
                  AND tombstoned=0 ORDER BY id
                """,
                (tenant_id, user_id),
            ).fetchall()]
        return [
            self.tombstone_memory_atomic(
                memory_id, tenant_id=tenant_id, user_id=user_id,
                idempotency_key=f"{idempotency_prefix}:{memory_id}",
            )
            for memory_id in ids
        ]

    @staticmethod
    def _require_valid_lease(
        connection: sqlite3.Connection, run_id: str, lease_token: str, now: str
    ) -> sqlite3.Row:
        lease = connection.execute(
            "SELECT * FROM run_leases WHERE run_id=?", (run_id,)
        ).fetchone()
        if lease is None or lease["lease_token"] != lease_token or lease["expires_at"] <= now:
            raise PermissionError("lease token mismatch or lease expired")
        return lease

    def persist_document_ingestion(
        self,
        *,
        source: DocumentSource,
        normalized_text: str,
        content_sha256: str,
        version_id: str,
        chunks: list[DocumentChunk],
        embedding_profile_id: str,
        index_version: str,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        document_id = "doc_" + hashlib.sha256(
            f"{source.source_uri}:{source.access_scope}".encode("utf-8")
        ).hexdigest()[:32]
        job_id = "ing_" + hashlib.sha256(
            f"{version_id}:{embedding_profile_id}:{index_version}".encode("utf-8")
        ).hexdigest()[:32]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM document_versions WHERE id=?", (version_id,)
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    INSERT INTO ingestion_jobs(
                        id,document_version_id,embedding_profile_id,index_version,status,
                        attempt,error,created_at,updated_at
                    ) VALUES (?,?,?,?, 'pending',0,NULL,?,?)
                    ON CONFLICT(document_version_id,embedding_profile_id,index_version) DO NOTHING
                    """,
                    (job_id, version_id, embedding_profile_id, index_version, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM document_versions WHERE id=?", (version_id,)
                ).fetchone()
                connection.commit()
                return dict(row), False
            connection.execute(
                """
                INSERT INTO documents(
                    id,source_uri,source_type,title,publisher,access_scope,company,
                    symbol,market,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_uri,access_scope) DO UPDATE SET
                    title=excluded.title,publisher=excluded.publisher,updated_at=excluded.updated_at
                """,
                (
                    document_id, _safe_public_url(source.source_uri), source.source_type,
                    source.title, source.publisher, source.access_scope, source.company,
                    source.symbol, source.market, now, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_versions(
                    id,document_id,content_sha256,source_version,mime_type,byte_size,
                    published_at,fetched_at,normalized_text,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    version_id, document_id, content_sha256, source.source_version,
                    source.mime_type, len(normalized_text.encode("utf-8")),
                    source.published_at, now, normalized_text, now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO document_chunks(
                    id,document_version_id,ordinal,section,page,text,content_sha256,
                    char_start,char_end,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        chunk.id, chunk.document_version_id, chunk.ordinal, chunk.section,
                        chunk.page, chunk.text, chunk.content_sha256, chunk.char_start,
                        chunk.char_end, now,
                    )
                    for chunk in chunks
                ],
            )
            connection.execute(
                """
                INSERT INTO ingestion_jobs(
                    id,document_version_id,embedding_profile_id,index_version,status,
                    attempt,error,created_at,updated_at
                ) VALUES (?,?,?,?, 'pending',0,NULL,?,?)
                """,
                (job_id, version_id, embedding_profile_id, index_version, now, now),
            )
            row = connection.execute(
                "SELECT * FROM document_versions WHERE id=?", (version_id,)
            ).fetchone()
            connection.commit()
            assert row is not None
            return dict(row), True

    def persist_verified_evidence(
        self,
        run_id: str,
        *,
        lease_token: str,
        evidence: list[EvidenceItem],
        claims: list[VerifiedClaim],
    ) -> None:
        now = utc_now()
        evidence_by_id = {item.id: item for item in evidence}
        from backend.schemas import ClaimCandidate
        from backend.verifier import ClaimVerifier

        reverified = ClaimVerifier().verify(
            [
                ClaimCandidate(
                    id=claim.id, run_id=claim.run_id, text=claim.text,
                    evidence_ids=claim.evidence_ids, period=claim.period,
                    unit=claim.unit, currency=claim.currency,
                )
                for claim in claims
            ],
            evidence,
            allowed_access_scopes={"public"},
        )
        if [item.model_dump() for item in reverified] != [item.model_dump() for item in claims]:
            raise ValueError("claims do not match deterministic verification")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "running":
                raise ValueError("verified evidence can only be saved for a running run")
            self._require_valid_lease(connection, run_id, lease_token, now)
            for item in evidence:
                if item.run_id != run_id:
                    raise ValueError("evidence belongs to another run")
                existing_evidence = connection.execute(
                    "SELECT * FROM evidence_items WHERE id=?", (item.id,)
                ).fetchone()
                if existing_evidence is not None and (
                    existing_evidence["run_id"] != run_id
                    or existing_evidence["content_sha256"] != item.content_sha256
                    or existing_evidence["source_uri"] != _safe_public_url(item.source_uri)
                    or existing_evidence["document_version_id"] != item.document_version_id
                    or existing_evidence["chunk_id"] != item.chunk_id
                    or existing_evidence["title"] != item.title
                    or existing_evidence["publisher"] != item.publisher
                    or existing_evidence["source_type"] != item.source_type
                    or existing_evidence["excerpt"] != redact_text(item.excerpt)
                    or existing_evidence["access_scope"] != item.access_scope
                    or int(existing_evidence["authority_tier"]) != item.authority_tier
                    or existing_evidence["published_at"] != item.published_at
                    or existing_evidence["page"] != item.page
                    or existing_evidence["section"] != item.section
                    or existing_evidence["company"] != item.company
                    or existing_evidence["period"] != item.period
                ):
                    raise ValueError("evidence id was reused with different identity")
                connection.execute(
                    """
                    INSERT INTO evidence_items(
                        id,run_id,document_version_id,chunk_id,source_uri,title,publisher,
                        source_type,excerpt,content_sha256,access_scope,authority_tier,
                        published_at,retrieved_at,page,section,company,period,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        item.id, run_id, item.document_version_id, item.chunk_id,
                        _safe_public_url(item.source_uri), item.title, item.publisher,
                        item.source_type, redact_text(item.excerpt), item.content_sha256,
                        item.access_scope, item.authority_tier, item.published_at,
                        item.retrieved_at, item.page, item.section, item.company,
                        item.period, now,
                    ),
                )
            for claim in claims:
                if claim.run_id != run_id:
                    raise ValueError("claim belongs to another run")
                digest = hashlib.sha256(claim.text.encode("utf-8")).hexdigest()
                existing_claim = connection.execute(
                    "SELECT * FROM claims WHERE id=?", (claim.id,)
                ).fetchone()
                if existing_claim is not None and (
                    existing_claim["run_id"] != run_id
                    or existing_claim["content_sha256"] != digest
                    or existing_claim["status"] != claim.status
                    or float(existing_claim["confidence"]) != claim.confidence
                    or existing_claim["period"] != claim.period
                    or existing_claim["unit"] != claim.unit
                    or existing_claim["currency"] != claim.currency
                    or json.loads(existing_claim["reason_codes_json"]) != claim.reason_codes
                ):
                    raise ValueError("claim id was reused with different identity")
                existing_links = {
                    row[0] for row in connection.execute(
                        "SELECT evidence_id FROM claim_evidence WHERE claim_id=?", (claim.id,)
                    ).fetchall()
                }
                if existing_claim is not None and existing_links != set(claim.evidence_ids):
                    raise ValueError("claim id was reused with different evidence links")
                connection.execute(
                    """
                    INSERT INTO claims(
                        id,run_id,text,content_sha256,status,confidence,period,unit,currency,
                        reason_codes_json,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        claim.id, run_id, claim.text, digest, claim.status, claim.confidence,
                        claim.period, claim.unit, claim.currency, _json(claim.reason_codes), now,
                    ),
                )
                for evidence_id in claim.evidence_ids:
                    if evidence_id not in evidence_by_id:
                        raise ValueError("claim references unknown evidence")
                    relation = {
                        "supported": "supports", "partially_supported": "partially_supports",
                        "conflicted": "conflicts", "unsupported": "partially_supports",
                    }[claim.status]
                    connection.execute(
                        """
                        INSERT INTO claim_evidence(claim_id,evidence_id,relation,created_at)
                        VALUES (?,?,?,?) ON CONFLICT(claim_id,evidence_id) DO NOTHING
                        """,
                        (claim.id, evidence_id, relation, now),
                    )

    def claim_ingestion_job(
        self, version_id: str, *, embedding_profile_id: str, index_version: str
    ) -> str | None:
        now = utc_now()
        from datetime import timedelta

        claim_token = str(uuid4())
        claim_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM ingestion_jobs WHERE document_version_id=?
                AND embedding_profile_id=? AND index_version=?
                """,
                (version_id, embedding_profile_id, index_version),
            ).fetchone()
            if row is None:
                raise KeyError("ingestion job does not exist")
            if row["status"] == "indexed":
                return None
            cursor = connection.execute(
                """
                UPDATE ingestion_jobs SET status='indexing',attempt=attempt+1,error=NULL,
                    claim_token_hash=?,claim_expires_at=?,updated_at=?
                WHERE id=? AND (
                    status IN ('pending','failed')
                    OR (status='indexing' AND claim_expires_at <= ?)
                )
                """,
                (_token_hash(claim_token), claim_expires_at, now, row["id"], now),
            )
            if cursor.rowcount != 1:
                raise ValueError("ingestion job is already claimed")
            return claim_token

    def finish_ingestion_job(
        self, version_id: str, *, embedding_profile_id: str, index_version: str,
        claim_token: str, success: bool, error: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_jobs SET status=?,error=?,claim_token_hash=NULL,
                    claim_expires_at=NULL,updated_at=?
                WHERE document_version_id=? AND embedding_profile_id=? AND index_version=?
                AND status='indexing' AND claim_token_hash=? AND claim_expires_at>?
                """,
                (
                    "indexed" if success else "failed",
                    None if success else redact_text(error or "indexing failed"), now,
                    version_id, embedding_profile_id, index_version,
                    _token_hash(claim_token), now,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("ingestion job claim was lost")

    def persist_report_snapshot_atomic(
        self,
        run_id: str,
        *,
        lease_token: str,
        generation_key: str,
        model: str,
        schema_version: int,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        canonical = _json(redact_value(snapshot))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        generation_id = "rgen_" + hashlib.sha256(
            f"{run_id}:{generation_key}".encode("utf-8")
        ).hexdigest()[:32]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "running":
                raise ValueError("report snapshot requires a running run")
            self._require_valid_lease(connection, run_id, lease_token, now)
            existing_generation = connection.execute(
                "SELECT * FROM report_generations WHERE run_id=? AND generation_key=?",
                (run_id, generation_key),
            ).fetchone()
            if existing_generation is None:
                connection.execute(
                    """
                    INSERT INTO report_generations(
                        id,run_id,generation_key,model,schema_version,status,created_at,updated_at
                    ) VALUES (?,?,?,?,?,'running',?,?)
                    """,
                    (generation_id, run_id, generation_key, model, schema_version, now, now),
                )
            elif (
                existing_generation["model"] != model
                or int(existing_generation["schema_version"]) != schema_version
            ):
                raise ValueError("generation key was reused with different identity")
            duplicate = connection.execute(
                "SELECT * FROM report_snapshots WHERE generation_id=? AND content_sha256=?",
                (generation_id, digest),
            ).fetchone()
            if duplicate is not None:
                return dict(duplicate)
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),-1)+1 FROM report_snapshots WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0])
            snapshot_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO report_snapshots(
                    id,generation_id,sequence,snapshot_json,content_sha256,created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (snapshot_id, generation_id, sequence, canonical, digest, now),
            )
            self._add_event(
                connection, run_id=run_id, kind="report.delta", step="reporting",
                status="running", progress=min(99, max(95, int(run["progress"]))),
                message="报告增量已持久化",
                payload={
                    "generation_id": generation_id, "sequence": sequence,
                    "snapshot_hash": digest, "snapshot": json.loads(canonical),
                },
            )
            row = connection.execute("SELECT * FROM report_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            assert row is not None
            return dict(row)

    def get_latest_report_snapshot(self, run_id: str, generation_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT s.* FROM report_snapshots s
                JOIN report_generations g ON g.id=s.generation_id
                WHERE g.run_id=? AND g.generation_key=? ORDER BY s.sequence DESC LIMIT 1
                """,
                (run_id, generation_key),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["snapshot"] = json.loads(item.pop("snapshot_json"))
        return item

    def complete_verified_report_atomic(
        self,
        run_id: str,
        *,
        lease_token: str,
        generation_key: str,
        markdown: str,
        report_json: dict[str, Any],
        citations: list[dict[str, Any]],
        degraded: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        canonical_report = _json(redact_value(report_json))
        safe_markdown = redact_text(markdown)
        digest = hashlib.sha256(safe_markdown.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] == "completed":
                existing_report = connection.execute(
                    "SELECT * FROM reports WHERE run_id=?", (run_id,)
                ).fetchone()
                existing_citations = [
                    dict(row) for row in connection.execute(
                        """
                        SELECT citation_number,claim_id,evidence_id FROM report_citations
                        WHERE report_id=? ORDER BY citation_number
                        """,
                        (existing_report["id"] if existing_report else "",),
                    ).fetchall()
                ]
                if (
                    existing_report is None
                    or existing_report["markdown"] != safe_markdown
                    or existing_report["report_json"] != canonical_report
                    or bool(existing_report["degraded"]) != bool(degraded)
                    or existing_citations != citations
                ):
                    raise ValueError("completed report replay has a different identity")
                current = self._get_task(connection, run_id)
                assert current is not None
                return current
            if run["status"] != "running":
                raise ValueError("verified report requires a running run")
            self._require_valid_lease(connection, run_id, lease_token, now)
            generation = connection.execute(
                "SELECT * FROM report_generations WHERE run_id=? AND generation_key=?",
                (run_id, generation_key),
            ).fetchone()
            if generation is None:
                raise ValueError("report generation has no persisted snapshot")
            latest_snapshot = connection.execute(
                """
                SELECT snapshot_json FROM report_snapshots WHERE generation_id=?
                ORDER BY sequence DESC LIMIT 1
                """,
                (generation["id"],),
            ).fetchone()
            expected_snapshot = {
                "markdown": safe_markdown,
                "report": json.loads(canonical_report),
                "complete": True,
            }
            if latest_snapshot is None or json.loads(latest_snapshot["snapshot_json"]) != expected_snapshot:
                raise ValueError("final report does not match the latest complete snapshot")
            report_citations = report_json.get("citations")
            if not isinstance(report_citations, list) or report_citations != citations:
                raise ValueError("report citations differ from the committed citation mapping")
            if [item.get("citation_number") for item in citations] != list(range(1, len(citations) + 1)):
                raise ValueError("report citation numbers must be contiguous")
            reportable_claims = {
                row[0] for row in connection.execute(
                    """
                    SELECT id FROM claims WHERE run_id=?
                    AND status IN ('supported','partially_supported')
                    AND EXISTS(SELECT 1 FROM claim_evidence ce WHERE ce.claim_id=claims.id)
                    """,
                    (run_id,),
                ).fetchall()
            }
            cited_claims = {item.get("claim_id") for item in citations}
            if cited_claims != reportable_claims:
                raise ValueError("final report must cite every and only reportable claim")
            # Reconstruct the trusted domain objects from the database and require
            # the supplied final payload to be exactly the deterministic rendering.
            from backend.reporting import CitationConstrainedReporter
            from backend.schemas import ReportDraft

            persisted_evidence: list[EvidenceItem] = []
            for row in connection.execute(
                "SELECT * FROM evidence_items WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall():
                persisted_evidence.append(EvidenceItem(
                    id=row["id"], run_id=row["run_id"],
                    document_version_id=row["document_version_id"], chunk_id=row["chunk_id"],
                    source_uri=row["source_uri"], title=row["title"], publisher=row["publisher"],
                    source_type=row["source_type"], excerpt=row["excerpt"],
                    content_sha256=row["content_sha256"], access_scope=row["access_scope"],
                    authority_tier=row["authority_tier"], retrieved_at=row["retrieved_at"],
                    published_at=row["published_at"], page=row["page"], section=row["section"],
                    company=row["company"], period=row["period"],
                ))
            persisted_claims: list[VerifiedClaim] = []
            for row in connection.execute(
                "SELECT * FROM claims WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall():
                links = [
                    item[0] for item in connection.execute(
                        "SELECT evidence_id FROM claim_evidence WHERE claim_id=? ORDER BY evidence_id",
                        (row["id"],),
                    ).fetchall()
                ]
                persisted_claims.append(VerifiedClaim(
                    id=row["id"], run_id=row["run_id"], text=row["text"],
                    status=row["status"], confidence=row["confidence"], evidence_ids=links,
                    reason_codes=json.loads(row["reason_codes_json"]), period=row["period"],
                    unit=row["unit"], currency=row["currency"],
                ))
            draft_payload = dict(json.loads(canonical_report))
            draft_payload.pop("citations", None)
            draft = ReportDraft.model_validate(draft_payload)
            expected_markdown, expected_json, expected_citations = CitationConstrainedReporter().render(
                draft, persisted_claims, persisted_evidence
            )
            if (
                safe_markdown != expected_markdown
                or json.loads(canonical_report) != expected_json
                or citations != expected_citations
                or bool(degraded) != draft.degraded
            ):
                raise ValueError("final report is not the validated deterministic rendering")
            for citation in citations:
                linked = connection.execute(
                    """
                    SELECT 1 FROM claim_evidence ce JOIN claims c ON c.id=ce.claim_id
                    JOIN evidence_items e ON e.id=ce.evidence_id
                    WHERE ce.claim_id=? AND ce.evidence_id=? AND c.run_id=? AND e.run_id=?
                    AND c.status IN ('supported','partially_supported')
                    """,
                    (citation["claim_id"], citation["evidence_id"], run_id, run_id),
                ).fetchone()
                if linked is None:
                    raise ValueError("report citation is not backed by verified evidence")
            report_id = "rep_" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
            connection.execute(
                """
                INSERT INTO reports(
                    id,run_id,generation_id,markdown,report_json,content_sha256,degraded,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (report_id, run_id, generation["id"], safe_markdown, canonical_report, digest, int(degraded), now),
            )
            connection.executemany(
                """
                INSERT INTO report_citations(report_id,citation_number,claim_id,evidence_id)
                VALUES (?,?,?,?)
                """,
                [(report_id, item["citation_number"], item["claim_id"], item["evidence_id"]) for item in citations],
            )
            connection.execute(
                "UPDATE report_generations SET status='completed',updated_at=? WHERE id=?",
                (now, generation["id"]),
            )
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),-1)+1 FROM checkpoints WHERE run_id=?", (run_id,),
            ).fetchone()[0])
            new_version = int(run["state_version"]) + 1
            frontier = json.loads(run["frontier_json"] or "{}")
            state = {
                "plan_version": int(frontier.get("plan_version") or 1),
                "frontier": frontier, "budget_used": int(run["budget_used"]),
                "report_committed": True, "report_id": report_id,
            }
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id,run_id,sequence,state_version,plan_version,frontier_json,state_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (str(uuid4()), run_id, sequence, new_version, state["plan_version"], _json(frontier), _json(state), now),
            )
            result = {"report_id": report_id, "markdown": safe_markdown, "report": json.loads(canonical_report), "degraded": degraded}
            cursor = connection.execute(
                """
                UPDATE agent_runs SET status='completed',current_step='completed',progress=100,
                    state_version=?,result_json=?,updated_at=?,error=NULL
                WHERE id=? AND status='running' AND state_version=?
                """,
                (new_version, _json(result), now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent verified report completion")
            self._add_event(
                connection, run_id=run_id, kind="report.completed", step="completed",
                status="completed", progress=100, message="已验证报告与引用已提交",
                payload={"report_id": report_id, "citation_count": len(citations), "degraded": degraded},
            )
            self._add_event(
                connection, run_id=run_id, kind="run.completed", step="completed",
                status="completed", progress=100, message="研究完成",
            )
            connection.execute("DELETE FROM run_leases WHERE run_id=?", (run_id,))
            completed = self._get_task(connection, run_id)
            assert completed is not None
            return completed

    def create_run_atomic(
        self,
        request: ResearchCreate,
        *,
        owner_id: str,
        idempotency_key: str,
        lease_token: str,
        lease_expires_at: str,
        case_id: str | None = None,
        intake_id: str | None = None,
        initial_plan: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str, bool]:
        validated_plan = (
            _validate_persisted_plan(initial_plan).model_dump()
            if initial_plan is not None else None
        )
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM agent_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_run = connection.execute(
                    "SELECT * FROM agent_runs WHERE id = ?", (existing["id"],)
                ).fetchone()
                assert existing_run is not None
                request_identity = (
                    request.company, request.symbol, request.market, request.question,
                    request.agent, request.depth,
                )
                stored_identity = (
                    existing_run["company"], existing_run["symbol"], existing_run["market"],
                    existing_run["question"], existing_run["agent"], existing_run["depth"],
                )
                if request_identity != stored_identity:
                    raise ValueError("idempotency key was already used with a different request")
                if case_id is not None and existing_run["case_id"] != case_id:
                    raise ValueError("idempotent run belongs to a different case")
                if intake_id is not None:
                    intake = connection.execute(
                        "SELECT run_id FROM research_intakes WHERE id = ?", (intake_id,)
                    ).fetchone()
                    if intake is None or intake["run_id"] != existing_run["id"]:
                        raise ValueError("idempotent run is not linked to the intake")
                lease = connection.execute(
                    "SELECT lease_token FROM run_leases WHERE run_id = ?",
                    (existing["id"],),
                ).fetchone()
                if (
                    lease is None
                    and existing_run["status"] not in TERMINAL_STATUSES
                    and existing_run["status"] != "paused"
                ):
                    raise RuntimeError("idempotent run exists without its initial lease")
                run = self._get_task(connection, existing["id"])
                if run is None:
                    raise KeyError(existing["id"])
                return run, str(lease["lease_token"]) if lease else "", False

            run_id = str(uuid4())
            target_case_id = case_id or str(uuid4())
            plan_id = str(uuid4())
            checkpoint_id = str(uuid4())
            title = f"{request.company}公司研究"
            plan = validated_plan or {"version": 1, "goal": request.question, "steps": []}
            if int(plan["version"]) != 1:
                raise ValueError("initial plan version must be 1")
            initial_steps = list(plan.get("steps") or [])
            frontier = {
                "plan_version": 1,
                "ready_step_ids": [
                    step["id"] for step in initial_steps if not step.get("dependencies")
                ],
                "running_step_ids": [],
                "blocked_step_ids": [
                    step["id"] for step in initial_steps if step.get("dependencies")
                ],
                "completed_step_ids": [],
            }
            state = {
                "goal": request.question,
                "plan_version": 1,
                "frontier": frontier,
                "budget_used": 0,
            }
            if case_id is None:
                connection.execute(
                    "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (target_case_id, request.company, request.symbol, request.market, title, now, now),
                )
            else:
                existing_case = connection.execute(
                    "SELECT * FROM cases WHERE id = ?", (case_id,)
                ).fetchone()
                if existing_case is None:
                    raise KeyError(case_id)
            connection.execute(
                """
                INSERT INTO agent_runs (
                    id, case_id, idempotency_key, company, symbol, market, question,
                    agent, depth, status, current_step, progress, state_version,
                    budget_used, frontier_json, recovery_required, created_at,
                    updated_at, result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'starting', 0, 1, 0, ?, 0, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    target_case_id,
                    idempotency_key,
                    request.company,
                    request.symbol,
                    request.market,
                    request.question,
                    request.agent,
                    request.depth,
                    _json(frontier),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO plans(id, run_id, version, plan_json, created_at) VALUES (?, ?, 1, ?, ?)",
                (plan_id, run_id, _json(plan), now),
            )
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, 0, 1, 1, ?, ?, ?)
                """,
                (checkpoint_id, run_id, _json(frontier), _json(state), now),
            )
            connection.execute(
                """
                INSERT INTO run_leases(run_id, owner_id, lease_token, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, owner_id, lease_token, lease_expires_at, now),
            )
            if intake_id is not None:
                intake_budget = connection.execute(
                    "SELECT budget_limit FROM research_intakes WHERE id = ?", (intake_id,)
                ).fetchone()
                if intake_budget is None:
                    raise KeyError(intake_id)
                if validated_plan is not None and sum(
                    int(step["estimated_cost"]) for step in validated_plan["steps"]
                ) > int(intake_budget["budget_limit"]):
                    raise ValueError("initial plan exceeds intake budget")
                cursor = connection.execute(
                    """
                    UPDATE research_intakes
                    SET status = 'running', run_id = ?, updated_at = ?
                    WHERE id = ? AND status = 'ready' AND run_id IS NULL
                    """,
                    (run_id, now, intake_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("research intake is not ready or already linked")
            self._add_event(
                connection,
                run_id=run_id,
                kind="run.started",
                step="planned" if initial_steps else "starting",
                status="running",
                progress=0,
                message="研究任务已创建",
                payload={"plan_version": 1, "step_count": len(initial_steps)},
            )
            run = self._get_task(connection, run_id)
            if run is None:
                raise KeyError(run_id)
            return run, lease_token, True

    def create_task(self, request: ResearchCreate) -> dict[str, Any]:
        # Compatibility helper for callers that have not moved to DurableRunner yet.
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        run, _token, _created = self.create_run_atomic(
            request,
            owner_id="legacy-inline-worker",
            idempotency_key=str(uuid4()),
            lease_token=str(uuid4()),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        return run

    def _get_task(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        task = dict(row)
        task["result"] = json.loads(task.pop("result_json")) if task["result_json"] else None
        task["frontier"] = json.loads(task.pop("frontier_json") or "{}")
        task["recovery_required"] = bool(task["recovery_required"])
        task["evidence"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT citation_number, title, publisher, url, source_type, excerpt, agent
                FROM evidence WHERE run_id = ? ORDER BY citation_number
                """,
                (run_id,),
            ).fetchall()
        ]
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._get_task(connection, task_id)

    def get_runtime_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = self._get_task(connection, run_id)
            if run is None:
                raise KeyError(run_id)
            plan_row = connection.execute(
                "SELECT * FROM plans WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            checkpoint_row = connection.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            lease_row = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            plan = dict(plan_row) if plan_row else None
            if plan:
                plan.update(json.loads(plan.pop("plan_json")))
            checkpoint = dict(checkpoint_row) if checkpoint_row else None
            if checkpoint:
                checkpoint["frontier"] = json.loads(checkpoint.pop("frontier_json"))
                checkpoint["state"] = json.loads(checkpoint.pop("state_json"))
            lease = dict(lease_row) if lease_row else None
            steps = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM run_steps WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                ).fetchall()
            ]
            tool_calls = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                ).fetchall()
            ]
            counts = {
                "steps": connection.execute(
                    "SELECT COUNT(*) FROM run_steps WHERE run_id = ?", (run_id,)
                ).fetchone()[0],
                "tool_calls": connection.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE run_id = ?", (run_id,)
                ).fetchone()[0],
                "checkpoints": connection.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE run_id = ?", (run_id,)
                ).fetchone()[0],
            }
        return {
            "run": run,
            "plan": plan,
            "checkpoint": checkpoint,
            "lease": lease,
            "events": self.list_events(run_id),
            "counts": counts,
            "steps": steps,
            "tool_calls": tool_calls,
        }

    def install_plan_atomic(
        self,
        run_id: str,
        *,
        lease_token: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        validated = _validate_persisted_plan(plan)
        plan = validated.model_dump()
        version = validated.version
        steps = plan["steps"]
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "running":
                raise ValueError(f"cannot install a plan in {run['status']}")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if lease is None or lease["lease_token"] != lease_token or lease["expires_at"] <= now:
                raise PermissionError("lease token mismatch or lease expired")
            latest_version = int(connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM plans WHERE run_id = ?", (run_id,)
            ).fetchone()[0])
            existing = connection.execute(
                "SELECT plan_json FROM plans WHERE run_id = ? AND version = ?",
                (run_id, version),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["plan_json"]) != plan:
                    raise ValueError("plan version was already used with different content")
                connection.commit()
                return self.get_runtime_snapshot(run_id)
            if version != latest_version + 1:
                raise ValueError("plan version must advance exactly once")
            intake = connection.execute(
                "SELECT id, replan_count FROM research_intakes WHERE run_id = ?", (run_id,)
            ).fetchone()
            completed = {
                row[0].removeprefix(f"{run_id}:")
                for row in connection.execute(
                    "SELECT id FROM run_steps WHERE run_id = ? AND status = 'succeeded'",
                    (run_id,),
                ).fetchall()
            }
            if intake is not None:
                budget_limit = int(connection.execute(
                    "SELECT budget_limit FROM research_intakes WHERE id = ?", (intake["id"],)
                ).fetchone()[0])
                remaining_plan_cost = sum(
                    item.estimated_cost for item in validated.steps if item.id not in completed
                )
                if int(run["budget_used"]) + remaining_plan_cost > budget_limit:
                    raise ValueError("replanned work exceeds intake budget")
                if int(intake["replan_count"]) >= 1:
                    raise ValueError("automatic replan limit reached")
                previous_plan = json.loads(connection.execute(
                    "SELECT plan_json FROM plans WHERE run_id = ? AND version = ?",
                    (run_id, latest_version),
                ).fetchone()[0])
            else:
                previous_plan = None
            if previous_plan is not None:
                old_steps = {item["id"]: item for item in previous_plan.get("steps", [])}
                new_steps = {item["id"]: item for item in steps}
                for completed_id in completed:
                    if old_steps.get(completed_id) != new_steps.get(completed_id):
                        raise ValueError("completed step definitions are immutable across replans")
            ready = [
                step["id"] for step in steps
                if step["id"] not in completed
                and set(step.get("dependencies") or []) <= completed
            ]
            blocked = [
                step["id"] for step in steps
                if step["id"] not in completed and step["id"] not in ready
            ]
            frontier = {
                "plan_version": version,
                "ready_step_ids": ready,
                "running_step_ids": [],
                "blocked_step_ids": blocked,
                "completed_step_ids": sorted(completed),
            }
            new_state_version = int(run["state_version"]) + 1
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO plans(id, run_id, version, plan_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid4()), run_id, version, _json(plan), now),
            )
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), run_id, sequence, new_state_version, version,
                    _json(frontier),
                    _json({
                        "goal": plan.get("goal"), "plan_version": version,
                        "frontier": frontier, "budget_used": int(run["budget_used"]),
                    }),
                    now,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET current_step = 'planned', progress = CASE WHEN progress < 5 THEN 5 ELSE progress END,
                    state_version = ?, frontier_json = ?, updated_at = ?
                WHERE id = ? AND state_version = ? AND status = 'running'
                """,
                (new_state_version, _json(frontier), now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent plan installation")
            if intake is not None:
                cursor = connection.execute(
                    "UPDATE research_intakes SET replan_count = replan_count + 1, updated_at = ? WHERE id = ? AND replan_count = ?",
                    (now, intake["id"], intake["replan_count"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("concurrent replan installation")
            self._add_event(
                connection, run_id=run_id, kind="plan.created", step="planned",
                status="running", progress=max(5, int(run["progress"])),
                message=f"研究计划 v{version} 已保存",
                payload={"plan_version": version, "step_count": len(steps)},
            )
        return self.get_runtime_snapshot(run_id)

    def record_execution_authorization(
        self,
        *,
        run_id: str,
        plan_version: int,
        step_id: str,
        tool_name: str,
        allowed: bool,
        reason_codes: list[str],
        estimated_cost: int,
        budget_before: int,
        capability_token: str | None = None,
        effective_cost: int | None = None,
        budget_limit: int | None = None,
        principal=None,
    ) -> dict[str, Any]:
        charged_cost = int(estimated_cost if effective_cost is None else effective_cost)
        operation = {
            "run_id": run_id, "plan_version": plan_version, "step_id": step_id,
            "tool_name": tool_name, "decision": "allow" if allowed else "deny",
            "reason_codes": reason_codes, "estimated_cost": estimated_cost,
            "budget_before": budget_before, "effective_cost": charged_cost,
        }
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT budget_used FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            reserved = int(connection.execute(
                "SELECT COALESCE(SUM(effective_cost), 0) FROM execution_authorizations WHERE run_id = ? AND status = 'reserved'",
                (run_id,),
            ).fetchone()[0])
            existing = connection.execute(
                "SELECT * FROM execution_authorizations WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()
            if existing is not None:
                decoded = dict(existing)
                decoded["reason_codes"] = json.loads(decoded.pop("reason_codes_json"))
                identity = {key: decoded[key] for key in operation}
                if identity != operation:
                    if existing["decision"] == "deny" and allowed:
                        if budget_limit is not None and (
                            int(run["budget_used"]) + reserved + charged_cost > int(budget_limit)
                        ):
                            raise ValueError("budget reservation exceeds run budget")
                        self._add_event(
                            connection, run_id=run_id, kind="authorization.reconsidered",
                            step=step_id, status="running", progress=0,
                            message="工具授权在用户确认或策略条件变化后重新评估",
                            payload={
                                "previous_decision": "deny",
                                "previous_reason_codes": decoded["reason_codes"],
                                "new_decision": "allow",
                            },
                        )
                        connection.execute(
                            """
                            UPDATE execution_authorizations
                            SET tool_name = ?, decision = 'allow', reason_codes_json = ?,
                                estimated_cost = ?, budget_before = ?, capability_token_hash = ?,
                                status = 'reserved', effective_cost = ? WHERE id = ? AND decision = 'deny'
                            """,
                            (tool_name, _json(reason_codes), estimated_cost, budget_before,
                             _token_hash(capability_token or ""), charged_cost, existing["id"]),
                        )
                        connection.execute(
                            "INSERT INTO execution_authorization_attempts VALUES (?, ?, 'allow', ?, ?, ?, ?)",
                            (str(uuid4()), existing["id"], _json(reason_codes), charged_cost,
                             budget_before, now),
                        )
                        row = connection.execute(
                            "SELECT * FROM execution_authorizations WHERE id = ?", (existing["id"],)
                        ).fetchone()
                        result = dict(row)
                        result["reason_codes"] = json.loads(result.pop("reason_codes_json"))
                        return result
                    raise ValueError("authorization was already recorded differently")
                if allowed and capability_token:
                    connection.execute(
                        "UPDATE execution_authorizations SET capability_token_hash = ? WHERE id = ? AND status = 'reserved'",
                        (_token_hash(capability_token), existing["id"]),
                    )
                    decoded["capability_token_hash"] = _token_hash(capability_token)
                return decoded
            if allowed and budget_limit is not None and (
                int(run["budget_used"]) + reserved + charged_cost > int(budget_limit)
            ):
                raise ValueError("budget reservation exceeds run budget")
            connection.execute(
                """
                INSERT INTO execution_authorizations(
                    id, run_id, plan_version, step_id, tool_name, decision,
                    reason_codes_json, estimated_cost, budget_before, created_at,
                    capability_token_hash, status, effective_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), run_id, plan_version, step_id, tool_name,
                    operation["decision"], _json(reason_codes), estimated_cost,
                    budget_before, now,
                    _token_hash(capability_token) if capability_token else None,
                    "reserved" if allowed else "denied", charged_cost,
                ),
            )
            authorization_id = connection.execute(
                "SELECT id FROM execution_authorizations WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO execution_authorization_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), authorization_id, operation["decision"], _json(reason_codes),
                 charged_cost, budget_before, now),
            )
            row = connection.execute(
                "SELECT * FROM execution_authorizations WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()
            assert row is not None
            decoded = dict(row)
            decoded["reason_codes"] = json.loads(decoded.pop("reason_codes_json"))
            return decoded

    def claim_tool_execution(
        self, *, run_id: str, plan_version: int, step_id: str, tool_name: str,
        lease_token: str, capability_token: str, idempotency_key: str,
        step_input: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        execution_token = str(uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if lease is None or lease["lease_token"] != lease_token or lease["expires_at"] <= now:
                raise PermissionError("lease token mismatch or lease expired")
            authorization = connection.execute(
                "SELECT * FROM execution_authorizations WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()
            if (
                authorization is None or authorization["decision"] != "allow"
                or authorization["tool_name"] != tool_name
                or authorization["status"] != "reserved"
                or authorization["capability_token_hash"] != _token_hash(capability_token)
            ):
                raise PermissionError("valid reserved execution capability required")
            existing = connection.execute(
                "SELECT * FROM tool_execution_claims WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()
            if existing is not None:
                decoded = dict(existing)
                if existing["status"] == "claimed" and existing["lease_token_hash"] != _token_hash(lease_token):
                    connection.execute(
                        "UPDATE tool_execution_claims SET execution_token_hash = ?, lease_token_hash = ?, updated_at = ? WHERE id = ? AND status = 'claimed'",
                        (_token_hash(execution_token), _token_hash(lease_token), now, existing["id"]),
                    )
                    decoded["execution_token_hash"] = _token_hash(execution_token)
                    decoded["lease_token_hash"] = _token_hash(lease_token)
                    decoded["execution_token"] = execution_token
                else:
                    decoded["execution_token"] = None
                decoded["input"] = json.loads(decoded.pop("input_json"))
                decoded["output"] = json.loads(decoded.pop("output_json")) if decoded["output_json"] else None
                decoded.pop("output_json", None)
                return decoded
            connection.execute(
                """
                INSERT INTO tool_execution_claims(
                    id, run_id, plan_version, step_id, tool_name, authorization_id,
                    execution_token_hash, lease_token_hash, idempotency_key, status,
                    input_json, output_json, error, duration_ms, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, NULL, NULL, NULL, ?, ?)
                """,
                (str(uuid4()), run_id, plan_version, step_id, tool_name, authorization["id"],
                 _token_hash(execution_token), _token_hash(lease_token), idempotency_key,
                 _json(redact_value(step_input)), now, now),
            )
            row = connection.execute(
                "SELECT * FROM tool_execution_claims WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()
            decoded = dict(row)
            decoded["input"] = json.loads(decoded.pop("input_json"))
            decoded["output"] = None
            decoded.pop("output_json", None)
            decoded["execution_token"] = execution_token
            return decoded

    def record_tool_observation(
        self, *, run_id: str, plan_version: int, step_id: str,
        lease_token: str, execution_token: str, output: dict[str, Any], duration_ms: int,
    ) -> dict[str, Any]:
        now = utc_now()
        sanitized = redact_value(output)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tool_execution_claims SET status = 'observed', output_json = ?,
                    duration_ms = ?, updated_at = ?
                WHERE run_id = ? AND plan_version = ? AND step_id = ? AND status = 'claimed'
                  AND execution_token_hash = ? AND lease_token_hash = ?
                """,
                (_json(sanitized), duration_ms, now, run_id, plan_version, step_id,
                 _token_hash(execution_token), _token_hash(lease_token)),
            )
            if cursor.rowcount != 1:
                raise PermissionError("tool execution claim was lost")
            return sanitized

    def get_tool_execution_claim(
        self, run_id: str, plan_version: int, step_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_execution_claims WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["input"] = json.loads(item.pop("input_json"))
        item["output"] = json.loads(item.pop("output_json")) if item["output_json"] else None
        item.pop("output_json", None)
        return item

    def issue_tool_commit_token(
        self, *, run_id: str, plan_version: int, step_id: str, lease_token: str
    ) -> str:
        now = utc_now()
        token = str(uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if lease is None or lease["lease_token"] != lease_token or lease["expires_at"] <= now:
                raise PermissionError("lease token mismatch or lease expired")
            cursor = connection.execute(
                """
                UPDATE tool_execution_claims
                SET execution_token_hash = ?, lease_token_hash = ?, updated_at = ?
                WHERE run_id = ? AND plan_version = ? AND step_id = ? AND status = 'observed'
                """,
                (_token_hash(token), _token_hash(lease_token), now, run_id, plan_version, step_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("observed tool claim required")
        return token

    def abandon_tool_claim(
        self, *, run_id: str, plan_version: int, step_id: str, lease_token: str,
        reason: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if lease is None or lease["lease_token"] != lease_token or lease["expires_at"] <= now:
                raise PermissionError("lease token mismatch or lease expired")
            claim = connection.execute(
                "SELECT * FROM tool_execution_claims WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                (run_id, plan_version, step_id),
            ).fetchone()
            if claim is None or claim["status"] not in {"claimed", "observed"}:
                raise ValueError("active tool claim required")
            connection.execute(
                "UPDATE tool_execution_claims SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (redact_text(reason), now, claim["id"]),
            )
            connection.execute(
                "UPDATE execution_authorizations SET status = 'released' WHERE id = ? AND status = 'reserved'",
                (claim["authorization_id"],),
            )

    def renew_lease(self, run_id: str, *, lease_token: str, expires_at: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE run_leases SET expires_at = ?, updated_at = ?
                WHERE run_id = ? AND lease_token = ? AND expires_at > ?
                """,
                (expires_at, now, run_id, lease_token, now),
            )
            if cursor.rowcount != 1:
                raise PermissionError("lease token mismatch or lease expired")
            row = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row)

    def take_over_expired_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_token: str,
        expires_at: str,
        grace_seconds: float = 0.0,
    ) -> tuple[dict[str, Any], str]:
        now = utc_now()
        # A lease must stay expired for at least ``grace_seconds`` before a new
        # owner may claim it. This gives a still-alive worker (whose heartbeat
        # merely lagged) time to renew or wind down instead of having its
        # renew/commit calls rejected by an immediate takeover.
        cutoff = (
            datetime.fromisoformat(now) - timedelta(seconds=max(0.0, grace_seconds))
        ).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] in TERMINAL_STATUSES or run["status"] == "paused":
                raise ValueError(f"cannot take over run in {run['status']}")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if lease is not None and lease["expires_at"] > cutoff:
                raise PermissionError("run still has an active lease (or is within grace)")
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'resuming', state_version = state_version + 1,
                    recovery_required = 1, updated_at = ?
                WHERE id = ? AND state_version = ?
                """,
                (now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent lease takeover")
            connection.execute(
                """
                INSERT INTO run_leases(run_id, owner_id, lease_token, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    lease_token = excluded.lease_token,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (run_id, owner_id, lease_token, expires_at, now),
            )
            self._add_event(
                connection,
                run_id=run_id,
                kind="run.resuming",
                step=run["current_step"],
                status="resuming",
                progress=run["progress"],
                message="运行租约已过期，正在从检查点恢复",
                payload={"recovery_required": True},
            )
            updated = self._get_task(connection, run_id)
            if updated is None:
                raise KeyError(run_id)
            return updated, str(run["status"])

    def commit_step_atomic(
        self,
        run_id: str,
        *,
        lease_token: str,
        step_id: str,
        kind: str,
        step_input: dict[str, Any],
        step_output: dict[str, Any],
        idempotency_key: str,
        frontier: dict[str, Any],
        progress: int,
        budget_delta: int,
        tool: dict[str, Any] | None = None,
        capability_token: str | None = None,
        tool_commit_token: str | None = None,
    ) -> dict[str, Any]:
        if not 0 <= progress < 100:
            raise ValueError("step progress must be between 0 and 99")
        if budget_delta < 0:
            raise ValueError("budget_delta cannot be negative")
        required_frontier_lists = {
            "ready_step_ids", "running_step_ids", "blocked_step_ids", "completed_step_ids"
        }
        if not all(isinstance(frontier.get(key, []), list) for key in required_frontier_lists):
            raise ValueError("frontier step collections must be lists")
        fingerprint = _commit_fingerprint({
            "step_id": step_id,
            "kind": kind,
            "step_input": step_input,
            "step_output": step_output,
            "frontier": frontier,
            "progress": progress,
            "budget_delta": budget_delta,
            "tool": tool,
        })
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] not in {"running", "pause_requested"}:
                raise ValueError(f"cannot commit a step in {run['status']}")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")
            existing = connection.execute(
                """
                SELECT id, kind, input_json, output_json, commit_fingerprint FROM run_steps
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["commit_fingerprint"] != fingerprint:
                    raise ValueError("step idempotency key was reused for a different operation")
                current = self._get_task(connection, run_id)
                if current is None:
                    raise KeyError(run_id)
                return current

            plan_version = int(frontier.get("plan_version") or 1)
            latest_plan_version = connection.execute(
                "SELECT MAX(version) FROM plans WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if plan_version != latest_plan_version:
                raise ValueError("frontier plan_version does not match the latest plan")
            plan_row = connection.execute(
                "SELECT plan_json FROM plans WHERE run_id = ? AND version = ?",
                (run_id, plan_version),
            ).fetchone()
            phase3_intake = connection.execute(
                "SELECT 1 FROM research_intakes WHERE run_id = ?", (run_id,)
            ).fetchone() is not None
            raw_plan = json.loads(plan_row["plan_json"])
            strict_plan_execution = phase3_intake or bool(raw_plan.get("steps"))
            plan = ResearchPlan.model_validate(raw_plan) if strict_plan_execution else None
            plan_steps = {item.id: item for item in plan.steps} if plan else {}
            planned_step = plan_steps.get(step_id)
            if strict_plan_execution and planned_step is None:
                raise ValueError("step does not belong to the latest plan")
            current_completed = {
                row[0].removeprefix(f"{run_id}:") for row in connection.execute(
                    "SELECT id FROM run_steps WHERE run_id = ? AND status = 'succeeded'", (run_id,)
                ).fetchall()
            }
            if planned_step is not None and not set(planned_step.dependencies) <= current_completed:
                raise ValueError("step dependencies are not completed")
            expected_completed = current_completed | {step_id}
            expected_ready = [
                item.id for item in plan.steps
                if item.id not in expected_completed and set(item.dependencies) <= expected_completed
            ] if plan else []
            expected_blocked = [
                item.id for item in plan.steps
                if item.id not in expected_completed and item.id not in expected_ready
            ] if plan else []
            expected_frontier = {
                "plan_version": plan_version, "ready_step_ids": expected_ready,
                "running_step_ids": [], "blocked_step_ids": expected_blocked,
                "completed_step_ids": sorted(expected_completed),
            }
            if planned_step is not None and frontier != expected_frontier:
                raise ValueError("frontier is not the canonical result of this commit")
            if planned_step is not None and (kind != planned_step.kind or step_input != planned_step.input):
                raise ValueError("step definition does not match the latest plan")
            if progress < int(run["progress"]):
                raise ValueError("step progress cannot move backwards")
            internal_step_id = f"{run_id}:{step_id}"
            cursor = connection.execute(
                """
                INSERT INTO run_steps(
                    id, run_id, plan_version, kind, status, input_json, output_json,
                    error, idempotency_key, commit_fingerprint, attempt, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'succeeded', ?, ?, NULL, ?, ?, 1, ?, ?)
                """,
                (
                    internal_step_id,
                    run_id,
                    plan_version,
                    kind,
                    _json(step_input),
                    _json(step_output),
                    idempotency_key,
                    fingerprint,
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent step commit")
            if strict_plan_execution and planned_step.kind == "tool" and tool is None:
                raise PermissionError("Phase 3 tool step requires an authorized tool observation")
            if tool is not None:
                authorization = connection.execute(
                    "SELECT * FROM execution_authorizations WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                    (run_id, plan_version, step_id),
                ).fetchone()
                claim = connection.execute(
                    "SELECT * FROM tool_execution_claims WHERE run_id = ? AND plan_version = ? AND step_id = ?",
                    (run_id, plan_version, step_id),
                ).fetchone()
                if strict_plan_execution and (
                    authorization is None or claim is None or tool_commit_token is None
                    or authorization["decision"] != "allow" or authorization["status"] != "reserved"
                    or claim["status"] != "observed" or claim["authorization_id"] != authorization["id"]
                    or claim["execution_token_hash"] != _token_hash(tool_commit_token)
                    or claim["lease_token_hash"] != _token_hash(lease_token)
                    or int(authorization["effective_cost"]) != int(budget_delta)
                ):
                    raise PermissionError("observed authorized tool claim required")
                if strict_plan_execution:
                    claim_input = json.loads(claim["input_json"])
                    claim_output = json.loads(claim["output_json"])
                    sanitized_plan_input = redact_value(planned_step.input)
                    sanitized_step_output = redact_value(step_output)
                    if not (
                        tool.get("name") == planned_step.tool_name == claim["tool_name"] == authorization["tool_name"]
                        and redact_value(tool.get("input", {})) == sanitized_plan_input == claim_input
                        and redact_value(tool.get("output", {})) == sanitized_step_output == claim_output
                        and tool.get("idempotency_key") == claim["idempotency_key"]
                        and int(tool.get("cost_units", -1)) == int(authorization["effective_cost"])
                    ):
                        raise PermissionError("tool commit does not match its plan, authorization and observation")
                connection.execute(
                    """
                    INSERT INTO tool_calls(
                        id, run_id, step_id, tool_name, tool_version, status,
                        input_json, output_json, error, duration_ms, cost_units,
                        idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run_id,
                        internal_step_id,
                        tool["name"],
                        tool.get("version", "1"),
                        _json(redact_value(tool.get("input", {}))),
                        _json(redact_value(tool.get("output", {}))),
                        tool.get("duration_ms"),
                        int(tool.get("cost_units", 0)),
                        tool["idempotency_key"],
                        now,
                        now,
                    ),
                )
                if strict_plan_execution:
                    connection.execute(
                        "UPDATE tool_execution_claims SET status = 'committed', updated_at = ? WHERE id = ? AND status = 'observed'",
                        (now, claim["id"]),
                    )
                    connection.execute(
                        "UPDATE execution_authorizations SET status = 'consumed' WHERE id = ? AND status = 'reserved'",
                        (authorization["id"],),
                    )

            checkpoint_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            target_status = "paused" if run["status"] == "pause_requested" else "running"
            new_version = int(run["state_version"]) + 1
            new_budget = int(run["budget_used"]) + int(budget_delta)
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, current_step = ?, progress = ?, state_version = ?,
                    budget_used = ?, frontier_json = ?, updated_at = ?
                WHERE id = ? AND status = ? AND state_version = ?
                """,
                (
                    target_status,
                    step_id,
                    progress,
                    new_version,
                    new_budget,
                    _json(frontier),
                    now,
                    run_id,
                    run["status"],
                    run["state_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent step commit")
            state = {
                "plan_version": plan_version,
                "frontier": frontier,
                "budget_used": new_budget,
                "last_step_id": step_id,
            }
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    run_id,
                    checkpoint_sequence,
                    new_version,
                    plan_version,
                    _json(frontier),
                    _json(state),
                    now,
                ),
            )
            self._add_event(
                connection,
                run_id=run_id,
                kind="step.completed",
                step=step_id,
                status=target_status,
                progress=progress,
                message=f"步骤 {step_id} 已完成并保存检查点",
                payload={"checkpoint_sequence": checkpoint_sequence},
            )
            if target_status == "paused":
                connection.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
                self._add_event(
                    connection,
                    run_id=run_id,
                    kind="run.paused",
                    step=step_id,
                    status="paused",
                    progress=progress,
                    message="研究已在安全检查点暂停",
                )
            updated = self._get_task(connection, run_id)
            if updated is None:
                raise KeyError(run_id)
            return updated

    def complete_run_atomic(
        self,
        run_id: str,
        *,
        lease_token: str,
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] == "completed":
                current = self._get_task(connection, run_id)
                if current is None:
                    raise KeyError(run_id)
                return current
            if run["status"] != "running":
                raise ValueError(f"cannot complete run in {run['status']}")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")

            connection.execute("DELETE FROM evidence WHERE run_id = ?", (run_id,))
            connection.executemany(
                """
                INSERT INTO evidence (
                    id, run_id, citation_number, title, publisher, url,
                    source_type, excerpt, agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()), run_id, item["citation_number"], item["title"],
                        item["publisher"], _safe_public_url(item["url"]), item["source_type"],
                        item["excerpt"], item["agent"], now,
                    )
                    for item in evidence
                ],
            )
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            new_version = int(run["state_version"]) + 1
            frontier = json.loads(run["frontier_json"] or "{}")
            state = {
                "plan_version": int(frontier.get("plan_version") or 1),
                "frontier": frontier,
                "budget_used": int(run["budget_used"]),
                "report_committed": True,
            }
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), run_id, sequence, new_version,
                    state["plan_version"], _json(frontier), _json(state), now,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'completed', current_step = 'completed', progress = 100,
                    state_version = ?, result_json = ?, updated_at = ?, error = NULL
                WHERE id = ? AND status = 'running' AND state_version = ?
                """,
                (new_version, _json(result), now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent completion")
            self._add_event(
                connection,
                run_id=run_id,
                kind="report.completed",
                step="completed",
                status="completed",
                progress=100,
                message="研究报告与证据已提交",
                payload={"evidence_count": len(evidence)},
            )
            self._add_event(
                connection,
                run_id=run_id,
                kind="run.completed",
                step="completed",
                status="completed",
                progress=100,
                message="研究完成",
            )
            connection.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
            completed = self._get_task(connection, run_id)
            if completed is None:
                raise KeyError(run_id)
            return completed

    def append_runtime_event(
        self,
        run_id: str,
        *,
        kind: str,
        step: str,
        progress: int,
        message: str,
        payload: dict[str, Any] | None = None,
        lease_token: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")
            self._add_event(
                connection,
                run_id=run_id,
                kind=kind,
                step=step,
                status=run["status"],
                progress=progress,
                message=message,
                payload=payload,
            )

    def cas_transition(
        self,
        run_id: str,
        *,
        from_statuses: Iterable[str],
        to_status: str,
        kind: str,
        message: str,
        step: str | None = None,
        expected_version: int | None = None,
        lease_token: str | None = None,
        delete_lease: bool = False,
        new_lease: tuple[str, str, str] | None = None,
        error: str | None = None,
        clear_recovery_required: bool = False,
    ) -> dict[str, Any]:
        statuses = tuple(from_statuses)
        if not statuses:
            raise ValueError("from_statuses cannot be empty")
        if to_status not in SIX_RUN_STATES:
            raise ValueError(f"invalid run status: {to_status}")
        if any((status, to_status) not in LEGAL_TRANSITIONS for status in statuses):
            raise ValueError(f"illegal state edge to {to_status}")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] not in statuses:
                raise ValueError(f"illegal transition from {row['status']} to {to_status}")
            if expected_version is not None and row["state_version"] != expected_version:
                raise ValueError("stale state version")
            if lease_token is not None:
                lease = connection.execute(
                    "SELECT lease_token, expires_at FROM run_leases WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if (
                    lease is None
                    or lease["lease_token"] != lease_token
                    or lease["expires_at"] <= now
                ):
                    raise PermissionError("lease token mismatch or lease expired")

            current_step = step or row["current_step"]
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, current_step = ?, state_version = state_version + 1,
                    updated_at = ?, error = COALESCE(?, error),
                    recovery_required = CASE WHEN ? THEN 0 ELSE recovery_required END
                WHERE id = ? AND status = ? AND state_version = ?
                """,
                (
                    to_status,
                    current_step,
                    now,
                    error,
                    1 if clear_recovery_required else 0,
                    run_id,
                    row["status"],
                    row["state_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent state transition")
            if delete_lease:
                connection.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
            if new_lease is not None:
                owner_id, token, expires_at = new_lease
                connection.execute(
                    """
                    INSERT INTO run_leases(run_id, owner_id, lease_token, expires_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        lease_token = excluded.lease_token,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, owner_id, token, expires_at, now),
                )
            self._add_event(
                connection,
                run_id=run_id,
                kind=kind,
                step=current_step,
                status=to_status,
                progress=int(row["progress"]),
                message=message,
            )
            run = self._get_task(connection, run_id)
            if run is None:
                raise KeyError(run_id)
            return run

    def list_recovery_candidates(self) -> list[str]:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT runs.id
                FROM agent_runs AS runs
                LEFT JOIN run_leases AS leases ON leases.run_id = runs.id
                WHERE runs.status IN ('running', 'pause_requested', 'resuming')
                  AND (leases.run_id IS NULL OR leases.expires_at <= ?)
                ORDER BY runs.updated_at, runs.id
                """,
                (now,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def expire_owner_leases(self, owner_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE run_leases SET expires_at = ?, updated_at = ? WHERE owner_id = ?",
                ("1970-01-01T00:00:00+00:00", utc_now(), owner_id),
            )

    def update_task_identity(
        self,
        task_id: str,
        *,
        company: str,
        symbol: str | None,
        market: str,
        lease_token: str,
    ) -> dict[str, Any]:
        company = company.strip()
        market = market.strip().upper()
        if not company:
            raise ValueError("company cannot be empty")
        if market not in {"CN", "HK", "US", "OTHER"}:
            market = "OTHER"
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (task_id,)
            ).fetchone()
            if (
                lease is None
                or lease["lease_token"] != lease_token
                or lease["expires_at"] <= now
            ):
                raise PermissionError("lease token mismatch or lease expired")
            run = connection.execute(
                "SELECT case_id FROM agent_runs WHERE id = ?", (task_id,)
            ).fetchone()
            if run is None:
                raise KeyError(task_id)
            connection.execute(
                "UPDATE agent_runs SET company = ?, symbol = ?, market = ?, updated_at = ? WHERE id = ?",
                (company, symbol or None, market, now, task_id),
            )
            connection.execute(
                "UPDATE cases SET company = ?, symbol = ?, market = ?, title = ?, updated_at = ? WHERE id = ?",
                (company, symbol or None, market, f"{company}公司研究", now, run["case_id"]),
            )
        resolved = self.get_task(task_id)
        if resolved is None:
            raise KeyError(task_id)
        return resolved

    def add_feedback(self, task_id: str, message: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        with self.connect() as connection:
            self._add_event(
                connection,
                run_id=task_id,
                kind="task.feedback",
                step=task["current_step"],
                status=task["status"],
                progress=task["progress"],
                message="已收到用户反馈",
                payload={"message": message},
            )
        result = self.get_task(task_id)
        if result is None:
            raise KeyError(task_id)
        return result

    def replace_evidence(
        self,
        task_id: str,
        items: list[dict[str, Any]],
        *,
        lease_token: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status FROM agent_runs WHERE id = ?", (task_id,)
            ).fetchone()
            if run is None:
                raise KeyError(task_id)
            if lease_token is None:
                raise PermissionError("evidence replacement requires a lease token")
            else:
                lease = connection.execute(
                    "SELECT * FROM run_leases WHERE run_id = ?", (task_id,)
                ).fetchone()
                if (
                    lease is None
                    or lease["lease_token"] != lease_token
                    or lease["expires_at"] <= now
                ):
                    raise PermissionError("lease token mismatch or lease expired")
            connection.execute("DELETE FROM evidence WHERE run_id = ?", (task_id,))
            connection.executemany(
                """
                INSERT INTO evidence (
                    id, run_id, citation_number, title, publisher, url,
                    source_type, excerpt, agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()),
                        task_id,
                        item["citation_number"],
                        item["title"],
                        item["publisher"],
                        _safe_public_url(item["url"]),
                        item["source_type"],
                        item["excerpt"],
                        item["agent"],
                        now,
                    )
                    for item in items
                ],
            )

    def enrich_completed_evidence(
        self,
        run_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "completed":
                raise ValueError("evidence enrichment is only allowed for completed runs")
            evidence_fields = (
                "citation_number", "title", "publisher", "url",
                "source_type", "excerpt", "agent",
            )
            before_items = [
                {field: row[field] for field in evidence_fields}
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE run_id = ? ORDER BY citation_number",
                    (run_id,),
                ).fetchall()
            ]
            normalized_items = [
                {**item, "url": _safe_public_url(item["url"])} for item in items
            ]
            before_hash = _commit_fingerprint({"evidence": before_items})
            after_hash = _commit_fingerprint({"evidence": normalized_items})
            connection.execute("DELETE FROM evidence WHERE run_id = ?", (run_id,))
            connection.executemany(
                """
                INSERT INTO evidence (
                    id, run_id, citation_number, title, publisher, url,
                    source_type, excerpt, agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()), run_id, item["citation_number"], item["title"],
                        item["publisher"], _safe_public_url(item["url"]), item["source_type"],
                        item["excerpt"], item["agent"], now,
                    )
                    for item in normalized_items
                ],
            )
            latest = connection.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if latest is None:
                raise ValueError("completed run is missing its checkpoint")
            previous_state = json.loads(latest["state_json"])
            new_state = {
                **previous_state,
                "evidence_enriched": True,
                "evidence_count": len(normalized_items),
                "evidence_before_hash": before_hash,
                "evidence_after_hash": after_hash,
            }
            new_version = int(run["state_version"]) + 1
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, run_id, sequence, state_version, plan_version,
                    frontier_json, state_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), run_id, int(latest["sequence"]) + 1,
                    new_version, latest["plan_version"], latest["frontier_json"],
                    _json(new_state), now,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE agent_runs SET state_version = ?, updated_at = ?
                WHERE id = ? AND status = 'completed' AND state_version = ?
                """,
                (new_version, now, run_id, run["state_version"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent evidence enrichment")
            self._add_event(
                connection,
                run_id=run_id,
                kind="evidence.enriched",
                step="completed",
                status="completed",
                progress=100,
                message="报告证据元数据已补充",
                payload={
                    "evidence_count": len(normalized_items),
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                },
            )
            enriched = self._get_task(connection, run_id)
            if enriched is None:
                raise KeyError(run_id)
            return enriched

    def get_completed_step_output(self, run_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT output_json FROM run_steps
                WHERE run_id = ? AND idempotency_key = ? AND status = 'succeeded'
                """,
                (run_id, idempotency_key),
            ).fetchone()
        return json.loads(row["output_json"]) if row and row["output_json"] else None

    def list_events(self, task_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id",
                (task_id, after_id),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["task_id"] = event["run_id"]
            event["payload"] = json.loads(event.pop("payload_json")) if event["payload_json"] else None
            events.append(event)
        return events

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return dict(row) if row else None

    def get_latest_task_for_case(
        self, case_id: str, *, statuses: Iterable[str] | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            self._require_case(connection, case_id)
            if statuses is None:
                row = connection.execute(
                    "SELECT id FROM agent_runs WHERE case_id = ? ORDER BY created_at DESC LIMIT 1",
                    (case_id,),
                ).fetchone()
            else:
                selected = tuple(statuses)
                if not selected:
                    return None
                placeholders = ",".join("?" for _ in selected)
                row = connection.execute(
                    f"""
                    SELECT id FROM agent_runs
                    WHERE case_id = ? AND status IN ({placeholders})
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (case_id, *selected),
                ).fetchone()
            return self._get_task(connection, row["id"]) if row else None

    def list_cases(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT cases.*, agent_runs.id AS latest_task_id,
                       agent_runs.status AS latest_status,
                       agent_runs.progress AS latest_progress
                FROM cases
                LEFT JOIN agent_runs ON agent_runs.id = (
                    SELECT id FROM agent_runs
                    WHERE case_id = cases.id ORDER BY created_at DESC LIMIT 1
                )
                ORDER BY cases.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _require_case(connection: sqlite3.Connection, case_id: str) -> None:
        if connection.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone() is None:
            raise KeyError(case_id)

    @staticmethod
    def _decode_turn(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["reason_codes"] = json.loads(item.pop("reason_codes_json") or "[]")
        return item

    def append_conversation_turn(
        self,
        case_id: str,
        *,
        turn_id: str,
        role: str,
        content: str,
        intent: str | None = None,
        reason_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid conversation role")
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("conversation content cannot be empty")
        codes = reason_codes or []
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            existing = connection.execute(
                "SELECT * FROM conversation_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["case_id"] != case_id
                    or existing["role"] != role
                    or existing["content"] != normalized_content
                    or existing["intent"] != intent
                    or json.loads(existing["reason_codes_json"] or "[]") != codes
                ):
                    raise ValueError("turn id was already used with different content")
                return self._decode_turn(existing)
            next_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM conversation_turns WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    id, case_id, sequence, role, content, intent, reason_codes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_id, case_id, next_sequence, role, normalized_content, intent, _json(codes), now),
            )
            connection.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
            row = connection.execute(
                "SELECT * FROM conversation_turns WHERE id = ?", (turn_id,)
            ).fetchone()
            assert row is not None
            return self._decode_turn(row)

    def list_conversation_turns(self, case_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            self._require_case(connection, case_id)
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM conversation_turns WHERE case_id = ? ORDER BY sequence",
                    (case_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM conversation_turns
                        WHERE case_id = ? ORDER BY sequence DESC LIMIT ?
                    ) ORDER BY sequence
                    """,
                    (case_id, max(0, limit)),
                ).fetchall()
        return [self._decode_turn(row) for row in rows]

    def replace_case_summary(
        self, case_id: str, summary: str, *, last_turn_sequence: int
    ) -> dict[str, Any]:
        normalized = summary.strip()
        if not normalized:
            raise ValueError("case summary cannot be empty")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            latest_turn = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM conversation_turns WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            previous_cursor = int(
                connection.execute(
                    "SELECT COALESCE(MAX(last_turn_sequence), 0) FROM case_summaries WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            if (
                last_turn_sequence < 0
                or last_turn_sequence > latest_turn
                or last_turn_sequence < previous_cursor
            ):
                raise ValueError("last_turn_sequence must reference persisted turns monotonically")
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM case_summaries WHERE case_id = ?",
                    (case_id,),
                ).fetchone()[0]
            )
            summary_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_summaries(
                    id, case_id, version, summary, last_turn_sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (summary_id, case_id, version, normalized, last_turn_sequence, now),
            )
            row = connection.execute("SELECT * FROM case_summaries WHERE id = ?", (summary_id,)).fetchone()
            assert row is not None
            return dict(row)

    def get_case_summary(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            self._require_case(connection, case_id)
            row = connection.execute(
                "SELECT * FROM case_summaries WHERE case_id = ? ORDER BY version DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _decode_confirmation(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        raw_value = item.pop("resolved_value_json")
        item["resolved_value"] = json.loads(raw_value) if raw_value else None
        return item

    def put_pending_confirmation(
        self,
        case_id: str,
        *,
        confirmation_id: str,
        kind: str,
        prompt: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> dict[str, Any]:
        now = utc_now()
        canonical_expiry = _canonical_utc(expires_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            existing = connection.execute(
                "SELECT * FROM pending_confirmations WHERE id = ?", (confirmation_id,)
            ).fetchone()
            if existing is not None:
                decoded = self._decode_confirmation(existing)
                if (
                    decoded["case_id"] != case_id or decoded["kind"] != kind
                    or decoded["prompt"] != prompt or decoded["payload"] != payload
                    or decoded["expires_at"] != canonical_expiry
                ):
                    raise ValueError("confirmation id was already used with different content")
                return decoded
            connection.execute(
                """
                UPDATE pending_confirmations SET status = 'superseded', updated_at = ?
                WHERE case_id = ? AND status = 'pending'
                """,
                (now, case_id),
            )
            status = "pending" if canonical_expiry > now else "expired"
            connection.execute(
                """
                INSERT INTO pending_confirmations(
                    id, case_id, kind, prompt, payload_json, status, expires_at,
                    resolved_value_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    confirmation_id, case_id, kind, prompt.strip(), _json(payload),
                    status, canonical_expiry, now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pending_confirmations WHERE id = ?", (confirmation_id,)
            ).fetchone()
            assert row is not None
            return self._decode_confirmation(row)

    def get_pending_confirmation(self, case_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            connection.execute(
                """
                UPDATE pending_confirmations SET status = 'expired', updated_at = ?
                WHERE case_id = ? AND status = 'pending' AND expires_at <= ?
                """,
                (now, case_id, now),
            )
            row = connection.execute(
                """
                SELECT * FROM pending_confirmations
                WHERE case_id = ? AND status = 'pending' AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (case_id, now),
            ).fetchone()
        return self._decode_confirmation(row) if row else None

    def resolve_pending_confirmation(
        self, case_id: str, *, confirmation_id: str, value: dict[str, Any]
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_case(connection, case_id)
            connection.execute(
                """
                UPDATE pending_confirmations SET status = 'expired', updated_at = ?
                WHERE id = ? AND case_id = ? AND status = 'pending' AND expires_at <= ?
                """,
                (now, confirmation_id, case_id, now),
            )
            cursor = connection.execute(
                """
                UPDATE pending_confirmations
                SET status = 'resolved', resolved_value_json = ?, updated_at = ?
                WHERE id = ? AND case_id = ? AND status = 'pending' AND expires_at > ?
                """,
                (_json(value), now, confirmation_id, case_id, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("confirmation is not pending")
            row = connection.execute(
                "SELECT * FROM pending_confirmations WHERE id = ?", (confirmation_id,)
            ).fetchone()
            assert row is not None
            return self._decode_confirmation(row)

    @staticmethod
    def _decode_route_request(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["decision"] = json.loads(item.pop("decision_json"))
        item["trace"] = json.loads(item.pop("trace_json"))
        return item

    def get_route_request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return self._decode_route_request(row) if row else None

    def save_route_request_result(
        self,
        request_id: str,
        *,
        case_id: str | None,
        message: str,
        decision: dict[str, Any],
        response: str,
        trace: list[str],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if case_id is not None:
                self._require_case(connection, case_id)
            existing = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                decoded = self._decode_route_request(existing)
                if decoded["case_id"] != case_id or decoded["message"] != message:
                    raise ValueError("request id was already used with a different route request")
                return decoded
            connection.execute(
                """
                INSERT INTO route_requests(
                    id, case_id, message, decision_json, response, trace_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, case_id, message, _json(decision), response, _json(trace), now),
            )
            row = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (request_id,)
            ).fetchone()
            assert row is not None
            return self._decode_route_request(row)

    @staticmethod
    def _decode_research_intake(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["candidates"] = json.loads(item.pop("candidates_json"))
        raw_entity = item.pop("resolved_entity_json")
        item["resolved_entity"] = json.loads(raw_entity) if raw_entity else None
        return item

    def create_research_intake(
        self,
        route_request_id: str,
        *,
        depth: str,
        budget_limit: int,
        resolution: dict[str, Any],
        confirmation_id: str | None = None,
        confirmation_expires_at: str | None = None,
    ) -> dict[str, Any]:
        if depth not in {"quick", "standard", "deep"}:
            raise ValueError("invalid research depth")
        if budget_limit <= 0:
            raise ValueError("budget_limit must be positive")
        resolution_status = resolution.get("status")
        candidates = list(resolution.get("candidates") or [])
        selected = resolution.get("selected")
        if resolution_status not in {"resolved", "ambiguous", "unresolved"}:
            raise ValueError("invalid entity resolution status")
        if resolution_status == "resolved" and not selected:
            raise ValueError("resolved intake requires a selected entity")
        if resolution_status == "ambiguous" and (
            len(candidates) < 2 or not confirmation_id or not confirmation_expires_at
        ):
            raise ValueError("ambiguous intake requires candidates and confirmation metadata")
        now = utc_now()
        canonical_expiry = (
            _canonical_utc(confirmation_expires_at) if confirmation_expires_at else None
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            route = connection.execute(
                "SELECT * FROM route_requests WHERE id = ?", (route_request_id,)
            ).fetchone()
            if route is None:
                raise KeyError(route_request_id)
            decision = json.loads(route["decision_json"])
            if (
                decision.get("intent") not in {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"}
                or decision.get("requires_planner") is not True
                or decision.get("external_research_allowed") is not False
            ):
                raise PermissionError("route is not eligible for research intake")
            existing = connection.execute(
                "SELECT * FROM research_intakes WHERE route_request_id = ?",
                (route_request_id,),
            ).fetchone()
            if existing is not None:
                decoded = self._decode_research_intake(existing)
                if (
                    decoded["message"] != route["message"]
                    or decoded["depth"] != depth
                    or int(decoded["budget_limit"]) != int(budget_limit)
                ):
                    raise ValueError("route request was already used for a different intake")
                confirmation = connection.execute(
                    "SELECT id FROM entity_confirmations WHERE intake_id = ?",
                    (decoded["id"],),
                ).fetchone()
                decoded["confirmation_id"] = confirmation["id"] if confirmation else None
                return decoded

            intake_id = str(uuid4())
            status = {
                "resolved": "ready",
                "ambiguous": "awaiting_confirmation",
                "unresolved": "needs_clarification",
            }[str(resolution_status)]
            connection.execute(
                """
                INSERT INTO research_intakes(
                    id, route_request_id, message, depth, budget_limit, status,
                    entity_query, candidates_json, resolved_entity_json,
                    run_id, replan_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
                """,
                (
                    intake_id, route_request_id, route["message"], depth, budget_limit,
                    status, resolution.get("query"), _json(candidates),
                    _json(selected) if selected else None, now, now,
                ),
            )
            if status == "awaiting_confirmation":
                connection.execute(
                    """
                    INSERT INTO entity_confirmations(
                        id, intake_id, status, candidates_json, selected_candidate_id,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, NULL, ?, ?, ?)
                    """,
                    (confirmation_id, intake_id, _json(candidates), canonical_expiry, now, now),
                )
            row = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            assert row is not None
            decoded = self._decode_research_intake(row)
            decoded["confirmation_id"] = confirmation_id
            return decoded

    def get_research_intake(self, intake_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            confirmation = connection.execute(
                "SELECT * FROM entity_confirmations WHERE intake_id = ?", (intake_id,)
            ).fetchone()
            if (
                confirmation is not None
                and confirmation["status"] == "pending"
                and confirmation["expires_at"] <= now
            ):
                connection.execute(
                    "UPDATE entity_confirmations SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, confirmation["id"]),
                )
                connection.execute(
                    "UPDATE research_intakes SET status = 'needs_clarification', updated_at = ? WHERE id = ? AND status = 'awaiting_confirmation'",
                    (now, intake_id),
                )
            row = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            if row is None:
                return None
            decoded = self._decode_research_intake(row)
            latest_confirmation = connection.execute(
                "SELECT id FROM entity_confirmations WHERE intake_id = ?", (intake_id,)
            ).fetchone()
            decoded["confirmation_id"] = latest_confirmation["id"] if latest_confirmation else None
            return decoded

    def get_research_intake_by_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_intakes WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            decoded = self._decode_research_intake(row)
            confirmation = connection.execute(
                "SELECT id FROM entity_confirmations WHERE intake_id = ?", (decoded["id"],)
            ).fetchone()
            decoded["confirmation_id"] = confirmation["id"] if confirmation else None
            return decoded

    def resolve_entity_confirmation(
        self,
        intake_id: str,
        *,
        candidate_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intake = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            if intake is None:
                raise KeyError(intake_id)
            confirmation = connection.execute(
                "SELECT * FROM entity_confirmations WHERE intake_id = ?", (intake_id,)
            ).fetchone()
            if confirmation is None:
                raise ValueError("intake has no entity confirmation")
            if confirmation["status"] == "resolved":
                if confirmation["selected_candidate_id"] != candidate_id:
                    raise ValueError("entity confirmation was already resolved differently")
                decoded = self._decode_research_intake(intake)
                decoded["confirmation_id"] = confirmation["id"]
                return decoded
            if confirmation["status"] != "pending" or confirmation["expires_at"] <= now:
                connection.execute(
                    "UPDATE entity_confirmations SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, confirmation["id"]),
                )
                connection.execute(
                    "UPDATE research_intakes SET status = 'needs_clarification', updated_at = ? WHERE id = ?",
                    (now, intake_id),
                )
                raise ValueError("entity confirmation expired")
            candidates = json.loads(confirmation["candidates_json"])
            selected = next(
                (item for item in candidates if item.get("candidate_id") == candidate_id),
                None,
            )
            if selected is None:
                raise ValueError("candidate does not belong to this confirmation")
            cursor = connection.execute(
                """
                UPDATE entity_confirmations
                SET status = 'resolved', selected_candidate_id = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (candidate_id, now, confirmation["id"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("concurrent entity confirmation")
            connection.execute(
                """
                UPDATE research_intakes
                SET status = 'ready', resolved_entity_json = ?, updated_at = ?
                WHERE id = ? AND status = 'awaiting_confirmation'
                """,
                (_json(selected), now, intake_id),
            )
            row = connection.execute(
                "SELECT * FROM research_intakes WHERE id = ?", (intake_id,)
            ).fetchone()
            assert row is not None
            decoded = self._decode_research_intake(row)
            decoded["confirmation_id"] = confirmation["id"]
            return decoded

    @staticmethod
    def _add_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        step: str,
        status: str,
        progress: int,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                run_id, kind, step, status, progress, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                kind,
                step,
                status,
                progress,
                message,
                _json(payload) if payload is not None else None,
                utc_now(),
            ),
        )
