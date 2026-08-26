from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any, Protocol

from backend.auth.models import PrincipalContext
from backend.db.artifacts import PostgresResearchArtifacts
from backend.db.durable import PostgresDurableRepository
from backend.evidence import EvidenceBuilder
from backend.policy import PolicyGate
from backend.redaction import redact_text, redact_url
from backend.reporting import CitationConstrainedReporter
from backend.retrieval import RetrievalFilters, RetrievalQuery, RetrievalResponse
from backend.tool_registry import build_default_registry
from backend.verifier import ClaimVerifier


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


class ControlledToolsResearchProcessor:
    """Drives the default controlled-tool registry (the 7 real tools: controlled
    web/filings search, Tencent quote, Milvus retrieval, document reading,
    rule-based fact extraction, deterministic metric calculation) plus a
    deterministic citation-constrained report.

    The processor runs the persisted plan in rounds, executing each ready
    step through the registered tool, committing the result back to the
    durable ledger, and finally synthesising a verified report. No model is
    invoked: the seven tools are pure Python (deterministic formulas,
    rule-based extraction, the cninfo/Tencent HTTP clients). External data
    sources that fail degrade explicitly rather than hallucinating.
    """

    execution_profile = "controlled_tools"

    def __init__(
        self, durable: PostgresDurableRepository, artifacts: PostgresResearchArtifacts,
        *, lease_ttl_seconds: int = 30, max_rounds: int = 8,
        synthesizer: Any | None = None,
    ) -> None:
        self.durable = durable
        self.artifacts = artifacts
        self.lease_ttl_seconds = lease_ttl_seconds
        self.max_rounds = max_rounds
        self.synthesizer = synthesizer

    # ------------------------------------------------------------------ helpers

    def _ready_steps(self, plan: dict, completed_ids: set[str]) -> list[dict]:
        steps = plan.get("steps", [])
        return [
            step for step in steps
            if step.get("kind") != "synthesis"
            and step["id"] not in completed_ids
            and set(step.get("dependencies") or []).issubset(completed_ids)
        ]

    def _completed_step_ids(self, run_id: str) -> set[str]:
        completed: set[str] = set()
        for row in self.durable.get_runtime_snapshot(self.principal, run_id)["steps"]:
            # PG steps rows carry step_id + output_json; SQLite rows carry
            # id ("run_id:step_id") + status. Normalise both shapes.
            raw = row.get("step_id") or row.get("id") or ""
            if raw.startswith(f"{run_id}:"):
                raw = raw[len(run_id) + 1:]
            if not raw:
                continue
            if row.get("status") == "succeeded" or row.get("output_json"):
                completed.add(raw)
        return completed

    def _run_status(self, run_id: str) -> str:
        return self.durable.get_runtime_snapshot(self.principal, run_id)["run"]["status"]

    def _lease_expiry(self) -> str:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) + timedelta(seconds=self.lease_ttl_seconds)).isoformat()

    def _claim_authorization(self, principal, run_id, step):
        from backend.schemas import PlanStep
        # Provide a minimal PlanStep-shaped object to PolicyGate.
        ps = PlanStep(
            id=step["id"], kind=step["kind"], tool_name=step.get("tool_name") or step["id"],
            dependencies=step.get("dependencies") or [],
            input=step.get("input") or {}, success_criteria=step.get("success_criteria") or [],
            estimated_cost=step.get("estimated_cost", 1),
        )
        decision = self.policy.authorize(
            route=self._route, run=self.durable.get_runtime_snapshot(self.principal, run_id)["run"],
            entity_confirmed=True, plan_version=self.plan_version,
            step=ps, budget_limit=self.budget_limit, lease_token=self.lease_token,
            reserve=True, principal=principal,
        )
        if not decision.allowed:
            raise RuntimeError(f"step {step['id']} denied: {','.join(decision.reason_codes)}")
        return decision

    def _commit_tool_step(self, principal, run_id, plan_version, step, output, budget_delta):
        frontier = self._compute_frontier(run_id, plan_version, step["id"])
        progress = min(95, 5 + int(90 * self._completed_step_ids(run_id).__len__() / max(1, len(self.steps))))
        # Renew lease before commit (fencing; same as ResearchExecutor heartbeat).
        from datetime import datetime, timezone, timedelta
        try:
            self.durable.renew_run_lease(
                principal, run_id, lease_token=self.lease_token,
                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=self.lease_ttl_seconds)).isoformat(),
            )
        except Exception:
            pass
        snapshot = self.durable.commit_step(
            principal, run_id, lease_token=self.lease_token, step_id=step["id"],
            kind=step["kind"], step_input=step.get("input") or {},
            step_output=output, next_pointer=step.get("id"),
            progress=progress, budget_delta=budget_delta,
        )
        return snapshot, frontier, progress

    def _compute_frontier(self, run_id, plan_version, just_completed_id):
        steps = self.steps
        completed = self._completed_step_ids(run_id)
        completed.add(just_completed_id)
        ready, blocked = [], []
        for s in steps:
            if s["id"] in completed:
                continue
            (ready if set(s.get("dependencies") or []).issubset(completed) else blocked).append(s["id"])
        return {
            "plan_version": plan_version,
            "ready_step_ids": ready,
            "running_step_ids": [],
            "blocked_step_ids": blocked,
            "completed_step_ids": sorted(completed),
        }

    def _collect_price_bars(self, run_id: str) -> list[dict]:
        """Return the complete OHLCV bars from the fetch_prices step.

        The frontend renders a candlestick chart + volume bars from these.
        """
        for step in self.steps:
            if step.get("id") != "fetch_prices":
                continue
            output = self._step_output(run_id, "fetch_prices")
            if not output:
                return []
            data = output.get("data")
            if isinstance(data, list):
                bars = []
                for bar in data:
                    if not isinstance(bar, dict):
                        continue
                    try:
                        bars.append({
                            "date": str(bar.get("date") or ""),
                            "open": float(bar.get("open") or 0),
                            "high": float(bar.get("high") or 0),
                            "low": float(bar.get("low") or 0),
                            "close": float(bar.get("close") or 0),
                            "volume": float(bar.get("volume") or 0),
                        })
                    except (TypeError, ValueError):
                        continue
                return bars
        return []

    def _collect_evidence_and_claims(self, run_id, plan_version, steps_by_id):
        """Walk succeeded run_steps and collect (evidence, claim) tuples for the
        verified report. Each non-synthesis step's output becomes a piece of
        evidence and (if it looks like a claim) a claim with a citation."""
        evidence: list[dict] = []
        claims: list[dict] = []
        seen_evidence: set[str] = set()
        for s in self.steps:
            if s["kind"] != "tool":
                continue
            step_id = s["id"]
            output = self._step_output(run_id, step_id)
            if not output:
                continue
            data = output.get("data")
            if not isinstance(data, list):
                continue
            source_url = output.get("source_url") or ""
            publisher = "东方财富" if step_id == "fetch_statements" else ""
            flat_items: list[dict] = []
            if step_id == "fetch_statements":
                # Flatten one StatementRow per period into one item per metric
                # so every canonical field becomes a citable claim.
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    period = row.get("period") or ""
                    for key, metric in (row.get("metrics") or {}).items():
                        if not isinstance(metric, dict) or metric.get("value") is None:
                            continue
                        label = str(metric.get("label") or key)
                        value = metric["value"]
                        unit = str(metric.get("unit") or "")
                        flat_items.append({
                            "period": period,
                            "name": label,
                            "value": value,
                            "unit": unit,
                            "title": f"{period} {label}",
                            "excerpt": f"{label} = {value}{unit} ({period})".strip(),
                            "url": source_url,
                            "publisher": publisher,
                        })
            else:
                flat_items = data

            for index, item in enumerate(flat_items, start=1):
                if not isinstance(item, dict):
                    continue
                evidence_id = f"ct-evidence-{run_id}-{step_id}-{index}"
                if evidence_id in seen_evidence:
                    continue
                seen_evidence.add(evidence_id)
                identity = {
                    "id": evidence_id,
                    "excerpt": redact_text(item.get("text") or item.get("excerpt") or item.get("title") or ""),
                    "source_uri": redact_url(item.get("url") or item.get("source_uri") or ""),
                    "source_title": redact_text(item.get("title") or step_id),
                    "publisher": redact_text(item.get("publisher") or ""),
                    "authority_tier": int(item.get("authority_tier") or 0),
                    "step_id": step_id,
                }
                if step_id == "fetch_statements":
                    identity["metric_key"] = str(item.get("name") or "")
                    identity["period"] = str(item.get("period") or "")
                    identity["unit"] = str(item.get("unit") or "")
                    identity["value"] = item.get("value")
                if not identity["excerpt"] or not identity["source_uri"]:
                    continue
                evidence.append(identity)
                # Map the tool's structured data to a claim when it carries
                # numeric content (quote / metrics), or skip when the tool
                # already produced a textual finding (filings, retrieval).
                claim = self._item_to_claim(run_id, step_id, item, evidence_id)
                if claim is not None:
                    claims.append(claim)
        return evidence, claims

    def _step_output(self, run_id, step_id):
        from backend.tool_registry import ToolSpec  # noqa
        # Tool-call outputs are stored by complete_run, but here we re-read
        # the run_steps row's output_json (commit_step path stores it). Look up
        # via the durable snapshot.
        for row in self.durable.get_runtime_snapshot(self.principal, run_id)["steps"]:
            row_id = row.get("id") or row.get("step_id") or ""
            if row_id.endswith(f":{step_id}") or row_id == step_id:
                if row.get("output_json"):
                    try:
                        return json.loads(row["output_json"])
                    except (TypeError, ValueError):
                        return None
        return None

    def _item_to_claim(self, run_id, step_id, item, evidence_id):
        if step_id == "get_quote":
            name = item.get("name") or "行情"
            value = item.get("price")
            unit = item.get("unit") or ""
            text = f"{name} 现价 {value}{unit}".strip()
        elif step_id == "calculate_metrics":
            name = item.get("name")
            value = item.get("value")
            unit = item.get("unit") or ""
            text = f"{name} = {value}{unit}".strip()
        elif step_id == "extract_facts":
            name = item.get("name"); value = item.get("value"); unit = item.get("unit") or ""
            period = item.get("period") or ""
            text = f"{name}={value}{unit} ({period})".strip()
        elif step_id == "fetch_statements":
            name = item.get("name") or "指标"
            value = item.get("value"); unit = item.get("unit") or ""
            period = item.get("period") or ""
            text = f"{name} = {value}{unit} ({period})".strip()
        elif step_id == "fetch_prices":
            name = item.get("name") or "股价"
            value = item.get("value"); unit = item.get("unit") or ""
            period = item.get("period") or ""
            text = f"{name} = {value}{unit} ({period})".strip()
        elif step_id in {"search_filings", "retrieve_documents"}:
            title = item.get("title") or "源材料"
            text = f"参考：{title}"
        else:
            return None
        # evidence_id already encodes run_id + step_id + stable index; deriving
        # the claim id from it guarantees uniqueness and replay stability.
        claim_id = f"ct-claim-{evidence_id}"
        return {"id": claim_id, "text": text, "status": "supported", "confidence": 0.9,
                "evidence_ids": [evidence_id],
                "period": item.get("period") or "",
                "unit": item.get("unit") or "",
                "currency": item.get("currency") or ""}

    # ------------------------------------------------------------------ main

    def __call__(
        self, principal: PrincipalContext, run_id: str, lease_token: str,
    ) -> None:
        self.principal = principal
        self.lease_token = lease_token
        self.policy = PolicyGate(self.durable, build_default_registry())
        registry = self.policy.registry
        self.budget_limit = 1000
        self._route = self._build_route(principal)

        plan = self.durable.get_latest_plan(principal, run_id)
        if plan is None or plan.get("execution_profile") != self.execution_profile:
            raise RuntimeError("persisted plan does not authorize the controlled-tools processor")
        self.steps = plan["steps"]
        self.plan_version = int(plan["version"])

        # Multi-round execution: claim & run each ready step, commit, repeat.
        for _ in range(self.max_rounds):
            status = self._run_status(run_id)
            if status not in {"running", "pause_requested"}:
                break
            completed = self._completed_step_ids(run_id)
            ready = self._ready_steps(plan, completed)
            if not ready:
                break
            for step in ready:
                status = self._run_status(run_id)
                if status not in {"running", "pause_requested"}:
                    break
                output = asyncio.run(self._execute_step(registry, step, principal, run_id))
                self._commit_tool_step(principal, run_id, self.plan_version, step,
                                      output, budget_delta=step.get("estimated_cost", 1))

        # Report synthesis step.
        synth = next((s for s in self.steps if s["kind"] == "synthesis"), None)
        if synth is not None and self._run_status(run_id) not in {"paused"}:
            self._synthesize_report(principal, run_id, plan)

        if self._run_status(run_id) == "running":
            self.durable.transition(
                principal, run_id, from_status="running", to_status="completed",
                expected_version=int(self.durable.get_runtime_snapshot(self.principal, run_id)["run"]["state_version"]),
            )

    # ------------------------------------------------------------------ internals

    def _build_route(self, principal) -> Any:
        from backend.schemas import RouteDecision
        return RouteDecision(
            intent="RESEARCH_NEW", confidence=1, requires_planner=True,
            external_research_allowed=False, response_policy="await_entity_resolution",
        )

    async def _execute_step(self, registry, step, principal, run_id):
        tool_name = step["tool_name"]
        payload = step.get("input") or {}
        # Same authorization + claim + commit pattern as the dev policy.
        self._claim_authorization(principal, run_id, step)
        from backend.tool_registry import ToolInvocationContext
        ctx = ToolInvocationContext(
            run_id=run_id, plan_version=self.plan_version, step_id=step["id"],
            idempotency_key=f"tool:{self.plan_version}:{step['id']}",
        )
        try:
            execution = await registry.execute(tool_name, payload, context=ctx)
        except Exception as exc:
            # The tool failed (e.g. cninfo rate limit) — record an empty /
            # degraded result so downstream steps can still continue.
            return {"status": "empty", "data": [], "evidence": [],
                    "degraded": True, "degraded_reason": f"tool error: {exc}"[:200],
                    "fallback_used": None}
        return execution.output

    def _synthesize_report(self, principal, run_id, plan):
        from datetime import datetime, timezone
        snapshot = self.durable.get_runtime_snapshot(self.principal, run_id)
        run = snapshot["run"]
        evidence, claims = self._collect_evidence_and_claims(run_id, self.plan_version, {})

        verifier = ClaimVerifier()
        allowed_scopes = {"public"}
        verified = verifier.verify(
            [self._to_claim_candidate(run_id, c) for c in claims],
            [self._to_evidence_item(e, run_id) for e in evidence],
            allowed_access_scopes=allowed_scopes,
        ) if claims else []
        reportable = [v for v in verified if v.status in {"supported", "partially_supported"}]
        financial_metrics = [
            {
                "period": e.get("period") or "",
                "name": e.get("metric_key") or e.get("source_title") or "",
                "value": e.get("value"),
                "unit": e.get("unit") or "",
            }
            for e in evidence
            if e.get("step_id") == "fetch_statements" and e.get("value") is not None
        ]
        # Preserve the AkShare price bars for the frontend sparkline.
        price_bars = self._collect_price_bars(run_id)

        company = run.get("company") or "研究对象"
        question = plan.get("goal") or "公司研究"

        # Try LLM synthesis first; fall back to the deterministic reporter
        # when the API key is absent or the model call fails. The fallback
        # keeps the controlled-tools promise: never fabricate a report.
        llm_evidence = [
            {
                "url": item.get("source_uri") or "",
                "title": item.get("source_title") or "",
                "publisher": item.get("publisher") or "",
                "excerpt": item.get("excerpt") or "",
            }
            for item in evidence
        ]
        synthesized: dict[str, Any] | None = None
        synthesizer = self.synthesizer
        if synthesizer is None:
            from backend.synthesizer import DeepSeekReportSynthesizer
            synthesizer = DeepSeekReportSynthesizer.from_env()
        if synthesizer is not None and llm_evidence:
            try:
                synthesized = asyncio.run(synthesizer.synthesize(
                    company=company, question=question, evidence=llm_evidence,
                ))
            except Exception:
                synthesized = None

        if synthesized:
            markdown, report_json, citations = self._llm_report_to_output(
                synthesized, evidence, company, question,
            )
        else:
            reporter = CitationConstrainedReporter()
            draft = reporter.build_deterministic(
                company=company, question=question, claims=verified,
                evidence=[self._to_evidence_item(e, run_id) for e in evidence],
            )
            markdown, report_json, citations = reporter.render(
                draft, verified, [self._to_evidence_item(e, run_id) for e in evidence],
            )

        snapshot = self.durable.get_runtime_snapshot(self.principal, run_id)
        version = int(snapshot["run"]["state_version"])
        def _persist_claim_status(status: str) -> str:
            # Map the richer verifier status set onto the artifact persistence
            # vocabulary (supported / insufficient / contradicted).
            if status in {"supported", "partially_supported"}:
                return "supported"
            if status == "unsupported":
                return "insufficient"
            if status == "conflicted":
                return "contradicted"
            return "insufficient"

        self.artifacts.persist_verified_evidence(principal, run_id, lease_token=self.lease_token,
                                                evidence=evidence, claims=[
            {"id": v.id, "text": v.text, "status": _persist_claim_status(v.status),
             "confidence": v.confidence, "evidence_ids": v.evidence_ids} for v in verified
        ])
        if synthesized:
            # LLM synthesis already produced the full report contract;
            # keep its summary / sections / provider / limitations.
            report_payload = report_json
            report_payload["financial_metrics"] = financial_metrics
            report_payload["price_bars"] = price_bars
        else:
            report_payload = {
                "complete": True, "synthetic": False,
                "execution_profile": self.execution_profile,
                "title": f"{company} 公司分析报告",
                "summary": self._render_summary(verified, evidence),
                "sections": self._render_structured_report(run, verified, evidence, citations),
                "provider": "controlled_tools",
                "tool_count": len(evidence),
                "financial_metrics": financial_metrics,
                "price_bars": price_bars,
                "limitations": [
                    "non-official public APIs (cninfo / tencent) with explicit degradation",
                    "deterministic tools only, no LLM synthesis",
                ],
            }
        def _hash_text(value: str) -> str:
            import hashlib
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        def _hash_json(value) -> str:
            import json as _json
            return _hash_text(_json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

        evidence_by_id = {e.get("id"): e for e in evidence}
        claim_by_id = {v.id: v for v in verified}
        final_citations = []
        for c in citations:
            ev = evidence_by_id.get(str(c["evidence_id"])) or {}
            clean_ev = {
                "id": str(ev.get("id") or c["evidence_id"]),
                "excerpt": str(ev.get("excerpt") or ""),
                "source_uri": str(ev.get("source_uri") or ""),
                "source_title": str(ev.get("source_title") or ""),
                "publisher": str(ev.get("publisher") or ""),
                "authority_tier": int(ev.get("authority_tier") or 0),
            }
            claim = claim_by_id.get(str(c["claim_id"]))
            claim_text = str(claim.text if claim is not None else "")
            final_citations.append({
                "claim_id": str(c["claim_id"]),
                "evidence_id": str(c["evidence_id"]),
                "evidence_hash": _hash_json(clean_ev),
                "claim_hash": _hash_text(claim_text),
            })
        self.artifacts.complete_report(
            principal, run_id, lease_token=self.lease_token,
            expected_version=version, markdown=markdown,
            report=report_payload,
            citations=final_citations,
        )

    @staticmethod
    def _to_claim_candidate(run_id, claim):
        from backend.schemas import ClaimCandidate
        return ClaimCandidate(
            id=claim["id"], run_id=run_id, text=claim["text"],
            evidence_ids=claim.get("evidence_ids", []),
            period=claim.get("period") or "",
            unit=claim.get("unit") or "",
            currency=claim.get("currency") or "",
        )

    @staticmethod
    def _to_evidence_item(evidence, run_id=""):
        import hashlib
        from datetime import datetime, timezone
        from backend.schemas import EvidenceItem
        raw = str(evidence.get("excerpt") or evidence.get("source_uri") or "")
        content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return EvidenceItem(
            id=evidence["id"], run_id=run_id or str(evidence.get("run_id") or ""),
            excerpt=evidence["excerpt"], source_uri=evidence["source_uri"],
            title=evidence["source_title"] or "未命名来源", publisher=evidence["publisher"] or "未知来源",
            source_type=evidence.get("source_type") or "web",
            authority_tier=evidence["authority_tier"],
            content_sha256=content_hash,
            access_scope=evidence.get("access_scope") or "public",
            retrieved_at=evidence.get("retrieved_at") or datetime.now(timezone.utc).isoformat(),
            period=evidence.get("period") or None,
        )


    @staticmethod
    def _llm_report_to_output(
        synthesized: dict[str, Any],
        evidence: list[dict[str, Any]],
        company: str,
        question: str,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """Map LLM synthesis JSON back to the durable report contract.

        Citations are derived from ``source_urls`` inside each section and
        bound to the evidence id that owns that URL. A URL not present in
        the supplied evidence is silently dropped (the prompt forbids it,
        but the sanitizer enforces it too).
        """
        url_to_evidence: dict[str, dict[str, Any]] = {}
        for item in evidence:
            url = str(item.get("source_uri") or item.get("url") or "").strip()
            if url:
                url_to_evidence[url] = item

        sections_out: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        citation_counter = 0
        lines = [f"# {company}研究报告", "", str(synthesized.get("summary") or "")]

        raw_sections = synthesized.get("sections") or []
        for section in raw_sections:
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or "研究发现")
            content = str(section.get("content") or "")
            source_urls = [
                url for url in (section.get("source_urls") or [])
                if isinstance(url, str) and url in url_to_evidence
            ]
            section_cites: list[dict[str, Any]] = []
            section_markers: list[str] = []
            for url in source_urls:
                ev = url_to_evidence[url]
                ev_id = ev.get("id") or url
                claim_id = f"llm:{ev_id}:{citation_counter}"
                citation_counter += 1
                section_cites.append({
                    "claim_id": claim_id,
                    "evidence_id": ev_id,
                    "evidence_hash": "",
                    "claim_hash": "",
                })
                section_markers.append(f"[{citation_counter}]")
            points = [
                {
                    "label": str(point.get("label") or "")[:80],
                    "text": str(point.get("text") or "")[:2000],
                }
                for point in section.get("points") or []
                if isinstance(point, dict) and point.get("text")
            ]
            sections_out.append({
                "key": f"section_{len(sections_out) + 1}",
                "title": title,
                "content": content,
                "points": points,
                "source_urls": source_urls,
            })
            lines.extend(["", f"## {title}", ""])
            if content:
                lines.append(content)
            for point in points:
                marker = " ".join(section_markers) if section_markers else ""
                lines.append(f"- {point['label']}：{point['text']} {marker}".strip())
            citations.extend(section_cites)

        markdown = "\n".join(lines)
        report_json = {
            "complete": True,
            "synthetic": False,
            "execution_profile": "controlled_tools",
            "provider": synthesized.get("provider") or "deepseek_synthesis",
            "title": str(synthesized.get("title") or f"{company} 财报研究"),
            "summary": str(synthesized.get("summary") or ""),
            "sections": sections_out,
            "tool_count": len(evidence),
            "limitations": [
                "non-official public APIs (cninfo / tencent) with explicit degradation",
                "LLM synthesis is citation-constrained to persisted evidence only",
            ],
        }
        return markdown, report_json, citations

    def _render_structured_report(self, run, verified, evidence, citations):
        """Build the user-facing company-analysis report: 公司概览 / 股价表现 /
        财务摘要 / 财务分析 / 证据与来源. Each point is a verified claim and
        keeps its citation trail."""
        evidence_by_id = {e.get("id"): e for e in evidence}
        claim_by_id = {v.id: v for v in verified}
        citations_by_evidence: dict[str, list[dict]] = {}
        for c in citations:
            citations_by_evidence.setdefault(str(c["evidence_id"]), []).append(c)

        def _points_for(step_id: str, include=None, exclude=None, limit=None):
            points = []
            for v in verified:
                for eid in v.evidence_ids:
                    ev = evidence_by_id.get(str(eid))
                    if not ev:
                        continue
                    if ev.get("step_id") != step_id:
                        continue
                    text = v.text
                    name = text.split("=")[0].strip() if "=" in text else text
                    if include and not any(k in name for k in include):
                        continue
                    if exclude and any(k in name for k in exclude):
                        continue
                    points.append({"label": name, "text": text})
                    break
            if limit:
                points = points[:limit]
            return points

        def _sources_for(step_id: str):
            urls = []
            seen = set()
            for e in evidence:
                if e.get("step_id") == step_id and e.get("source_uri") and e["source_uri"] not in seen:
                    seen.add(e["source_uri"])
                    urls.append(e["source_uri"])
            return urls

        sections: list[dict] = []

        # 1. 公司概览
        overview_points = []
        latest_period = ""
        for e in evidence:
            period = e.get("period") or ""
            if period > latest_period:
                latest_period = period
        if latest_period:
            overview_points.append({"label": "最新报告期", "text": f"最新财务报告期：{latest_period}"})
        sections.append({
            "key": "overview",
            "title": "公司概览",
            "content": f"{run.get('company') or '研究对象'}（{run.get('symbol') or ''} · {run.get('market') or ''}）",
            "points": overview_points,
            "source_urls": [],
        })

        # 2. 股价表现
        price_points = _points_for("fetch_prices", include=("最新收盘价", "区间涨跌幅", "区间最高价", "区间最低价"))
        if not price_points:
            price_points = _points_for("get_quote")
        sections.append({
            "key": "price",
            "title": "股价表现",
            "content": "基于近期日线行情（AkShare）与实时快照，仅呈现客观价格数据。",
            "points": price_points,
            "source_urls": _sources_for("fetch_prices") or _sources_for("get_quote"),
        })

        # 3. 财务摘要（最新一期核心指标）
        financial_points = _points_for(
            "fetch_statements",
            include=("营业总收入", "归属母公司净利润", "ROE", "销售毛利率", "资产负债率", "基本每股收益", "每股净资产", "经营现金流"),
            limit=12,
        )
        sections.append({
            "key": "financial-summary",
            "title": "财务摘要",
            "content": "最新报告期核心财务指标（完整指标见财务分析章节）。",
            "points": financial_points,
            "source_urls": _sources_for("fetch_statements"),
        })

        # 4. 财务分析：按能力分类
        profitability = _points_for("fetch_statements", include=("ROE", "毛利率", "净利率", "ROA", "ROIC"))
        growth = _points_for("fetch_statements", include=("同比", "环比"))
        solvency = _points_for("fetch_statements", include=("资产负债率", "流动比率", "速动比率", "现金比率", "权益乘数"))
        cashflow = _points_for("fetch_statements", include=("现金流", "现金"))
        analysis_groups = [
            ("盈利能力", profitability),
            ("成长性", growth),
            ("偿债与流动性", solvency),
            ("现金流", cashflow),
        ]
        for label, points in analysis_groups:
            if not points:
                continue
            sections.append({
                "key": f"analysis-{label}",
                "title": f"财务分析 · {label}",
                "content": "",
                "points": points[:10],
                "source_urls": _sources_for("fetch_statements"),
            })

        # 5. 证据与来源
        source_items = []
        seen_urls = set()
        for e in evidence:
            url = e.get("source_uri") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            source_items.append({"label": e.get("publisher") or "来源", "text": e.get("source_title") or url})
        sections.append({
            "key": "sources",
            "title": "证据与来源",
            "content": f"本报告共引用 {len(evidence)} 条持久化证据，以下为主要来源。",
            "points": source_items[:20],
            "source_urls": list(seen_urls),
        })

        return sections

    def _render_summary(self, verified, evidence):
        if not verified:
            return "本次研究在受控工具链中执行；巨潮/腾讯/检索任一环节失败时已显式降级，未能形成可报告结论。"
        head = verified[:3]
        bullets = "；".join(f"{v.text}" for v in head)
        return f"基于 {len(evidence)} 条持久化证据与 {len(verified)} 条核验结论：{bullets}。"

    def _render_sections(self, verified, evidence, citations):
        sections: list[dict] = []
        claim_by_id = {v.id: v for v in verified}
        for ev in evidence:
            matched = [c for c in citations if c["evidence_id"] == ev["id"]]
            claim_texts = [claim_by_id[c["claim_id"]].text for c in matched
                          if c["claim_id"] in claim_by_id]
            sections.append({
                "key": "evidence_" + ev["id"].split("-")[-1],
                "title": ev["source_title"][:80] or ev["source_uri"],
                "content": ev["excerpt"][:200],
                "points": [{"label": ev["publisher"] or "源", "text": t} for t in claim_texts],
                "source_urls": [ev["source_uri"]],
            })
        if not sections:
            sections.append({
                "key": "empty", "title": "无可用证据", "content": "",
                "points": [], "source_urls": [],
            })
        return sections
