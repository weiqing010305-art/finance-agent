from __future__ import annotations

import hashlib
import json
from typing import Protocol

from backend.auth.models import PrincipalContext
from backend.db.artifacts import PostgresResearchArtifacts
from backend.db.durable import PostgresDurableRepository
from backend.redaction import redact_text, redact_url
from backend.retrieval import RetrievalFilters, RetrievalQuery, RetrievalResponse


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SyntheticSmokeResearchProcessor:
    """Exercises durable/report gates without claiming to perform external research."""

    execution_profile = "synthetic_smoke"

    def __init__(self, durable: PostgresDurableRepository, artifacts: PostgresResearchArtifacts):
        self.durable = durable
        self.artifacts = artifacts

    def __call__(self, principal: PrincipalContext, run_id: str, lease_token: str) -> None:
        run = self.durable.get_run(principal, run_id)
        if run is None:
            raise RuntimeError("run not found")
        snapshot = self.durable.commit_step(
            principal, run_id, lease_token=lease_token, step_id="synthetic_smoke_gate",
            step_input={"execution_profile": self.execution_profile},
            step_output={
                "external_tools_called": False,
                "warning": "synthetic smoke output is not research evidence",
            },
            next_pointer="synthetic_report", progress=60, budget_delta=0,
        )
        if snapshot["run"]["status"] == "paused":
            return

        excerpt = redact_text(
            f"SYNTHETIC SMOKE RESULT for {run['company']}: the durable execution path completed; "
            "no external financial source was queried and this text must not be used for investment decisions."
        )
        evidence_id, claim_id = f"synthetic-evidence-{run_id}", f"synthetic-claim-{run_id}"
        source_uri = redact_url(f"https://synthetic.invalid/finscope/{run_id}")
        evidence_identity = {
            "id": evidence_id, "excerpt": excerpt, "source_uri": source_uri,
            "source_title": "FinScope synthetic smoke", "publisher": "FinScope",
            "authority_tier": 0,
        }
        evidence_hash = _sha(_canonical(evidence_identity))
        self.artifacts.persist_verified_evidence(
            principal, run_id, lease_token=lease_token,
            evidence=[evidence_identity],
            claims=[{
                "id": claim_id, "text": excerpt, "status": "supported",
                "confidence": 1.0, "evidence_ids": [evidence_id],
            }],
        )
        markdown = (
            "# Synthetic smoke report\n\n"
            f"{excerpt} [1]\n\n"
            "**Limitation:** This report validates orchestration and persistence only."
        )
        self.artifacts.complete_report(
            principal, run_id, lease_token=lease_token,
            expected_version=int(snapshot["run"]["state_version"]), markdown=markdown,
            report={
                "complete": True, "synthetic": True,
                "execution_profile": self.execution_profile,
                "limitations": ["no external tools", "no real financial evidence"],
            },
            citations=[{
                "claim_id": claim_id, "evidence_id": evidence_id,
                "evidence_hash": evidence_hash, "claim_hash": _sha(excerpt),
            }],
        )


class PrincipalRetriever(Protocol):
    def search(
        self, principal: PrincipalContext, request: RetrievalQuery,
    ) -> RetrievalResponse: ...


class FormalRealRagProcessor:
    """Authorized, extractive local RAG execution without LLM or external tools."""

    execution_profile = "real_rag_local"

    def __init__(
        self, durable: PostgresDurableRepository, artifacts: PostgresResearchArtifacts,
        retriever: PrincipalRetriever, *, embedding_profile_id: str, index_version: str,
    ) -> None:
        self.durable, self.artifacts, self.retriever = durable, artifacts, retriever
        self.embedding_profile_id, self.index_version = embedding_profile_id, index_version

    @staticmethod
    def _observation(response: RetrievalResponse) -> dict:
        hits = []
        for item in response.results:
            hits.append({
                "chunk_id": item.chunk_id,
                "document_id": item.document_id,
                "document_version_id": item.document_version_id,
                "text": redact_text(item.text),
                "title": redact_text(item.title),
                "source_uri": redact_url(item.source_uri),
                "publisher": redact_text(item.publisher),
                "authority_tier": int(item.authority_tier),
                "page": item.page,
                "access_scope": item.access_scope,
                "rank": item.rank,
            })
        return {
            "backend": response.backend, "mode": response.mode,
            "fusion": response.fusion, "degraded": response.degraded,
            "degraded_reason": response.degraded_reason, "hits": hits,
        }

    def __call__(self, principal: PrincipalContext, run_id: str, lease_token: str) -> None:
        run = self.durable.get_run(principal, run_id)
        plan = self.durable.get_latest_plan(principal, run_id)
        if run is None or plan is None:
            raise RuntimeError("run or plan not found")
        if plan.get("execution_profile") != self.execution_profile:
            raise RuntimeError("persisted plan does not authorize the real RAG processor")
        retrieval_step = next(
            (step for step in plan.get("steps", []) if step.get("id") == "retrieve_documents"),
            None,
        )
        if retrieval_step is None:
            raise RuntimeError("persisted plan lacks the authorized retrieval step")
        step_input = dict(retrieval_step.get("input") or {})
        completed = self.durable.get_completed_step(principal, run_id, "retrieve_documents")
        if completed is None:
            response = self.retriever.search(principal, RetrievalQuery(
                query=str(step_input.get("question") or run["question"]),
                top_k=min(10, max(1, int(step_input.get("top_k", 5)))),
                candidate_k=40,
                filters=RetrievalFilters(company=str(run["company"])),
                embedding_profile_id=self.embedding_profile_id,
                index_version=self.index_version,
            ))
            observation = self._observation(response)
        else:
            if completed["input"] != step_input:
                raise RuntimeError("persisted retrieval input does not match the current plan")
            observation = completed["output"]
        if not observation.get("hits"):
            raise RuntimeError("authorized document retrieval returned no evidence")
        if any(int(item.get("authority_tier", 0)) < 2 for item in observation["hits"]):
            raise RuntimeError("retrieved evidence does not meet the local authority threshold")
        snapshot = self.durable.commit_step(
            principal, run_id, lease_token=lease_token, step_id="retrieve_documents",
            step_input=step_input, step_output=observation,
            next_pointer="synthesize_verified_report", progress=65, budget_delta=2,
        )
        if snapshot["run"]["status"] == "paused":
            return

        evidence, claims, citations, sections = [], [], [], []
        for index, hit in enumerate(observation["hits"][:5], start=1):
            evidence_id = f"rag-evidence-{run_id}-{hit['chunk_id']}"
            claim_id = f"rag-claim-{run_id}-{hit['chunk_id']}"
            evidence_identity = {
                "id": evidence_id, "excerpt": hit["text"],
                "source_uri": hit["source_uri"],
                "source_title": hit["title"], "publisher": hit["publisher"],
                "authority_tier": int(hit["authority_tier"]),
            }
            evidence.append(evidence_identity)
            claims.append({
                "id": claim_id, "text": hit["text"], "status": "supported",
                "confidence": 0.9, "evidence_ids": [evidence_id],
            })
            citations.append({
                "claim_id": claim_id, "evidence_id": evidence_id,
                "evidence_hash": _sha(_canonical(evidence_identity)),
                "claim_hash": _sha(hit["text"]),
            })
            sections.append(f"{hit['text']} [{index}]")
        self.artifacts.persist_verified_evidence(
            principal, run_id, lease_token=lease_token, evidence=evidence, claims=claims,
        )
        markdown = (
            f"# Local authorized RAG report — {redact_text(str(run['company']))}\n\n"
            + "\n\n".join(sections)
            + "\n\n**Limitation:** This deterministic report uses a labelled local indexed fixture; "
              "it does not call external financial sources or an LLM."
        )
        self.artifacts.complete_report(
            principal, run_id, lease_token=lease_token,
            expected_version=int(snapshot["run"]["state_version"]), markdown=markdown,
            report={
                "complete": True, "synthetic": False,
                "execution_profile": self.execution_profile,
                "retrieval_backend": observation["backend"],
                "retrieval_mode": observation["mode"],
                "fixture": True,
                "limitations": ["local indexed fixture", "no external tools", "no LLM synthesis"],
            },
            citations=citations,
        )
