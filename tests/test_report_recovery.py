from __future__ import annotations

import sqlite3

import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner, RunConflict
from backend.evidence import EvidenceBuilder
from backend.reporting import CitationConstrainedReporter
from backend.schemas import ResearchCreate, VerifiedClaim


def _runtime(tmp_path):
    repo = Repository(tmp_path / "report.db"); repo.initialize(); runner = DurableRunner(repo)
    created = runner.create_run(
        ResearchCreate(company="腾讯", symbol="0700.HK", market="HK", question="分析收入增长情况"),
        owner_id="test", idempotency_key="report-run",
    )
    evidence = EvidenceBuilder().build_retrieval_items(created.run["id"], [{
        "text": "2024年收入增长10%。", "source_uri": "https://example.com/report",
        "title": "年报", "publisher": "交易所", "authority_tier": 5,
    }])[0]
    claim = VerifiedClaim(
        id="cl1", run_id=created.run["id"], text="2024年收入增长10%",
        status="supported", confidence=0.9, evidence_ids=[evidence.id],
        reason_codes=[], period="2024", unit="%",
    )
    reporter = CitationConstrainedReporter()
    draft = reporter.build_deterministic(
        company="腾讯", question="分析收入增长情况", claims=[claim], evidence=[evidence]
    )
    markdown, report_json, citations = reporter.render(draft, [claim], [evidence])
    return repo, runner, created, evidence, claim, markdown, report_json, citations


def test_snapshot_is_persisted_before_delta_event_and_duplicate_is_idempotent(tmp_path):
    repo, runner, created, evidence, claim, markdown, report_json, citations = _runtime(tmp_path)
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    first = runner.persist_report_snapshot(
        created.run["id"], lease_token=created.lease_token,
        generation_key="deterministic-v1", model="deterministic", schema_version=1,
        snapshot={"markdown": markdown, "report": report_json},
    )
    second = runner.persist_report_snapshot(
        created.run["id"], lease_token=created.lease_token,
        generation_key="deterministic-v1", model="deterministic", schema_version=1,
        snapshot={"markdown": markdown, "report": report_json},
    )
    assert first["id"] == second["id"]
    assert repo.get_latest_report_snapshot(created.run["id"], "deterministic-v1")["sequence"] == 0
    events = repo.list_events(created.run["id"])
    assert sum(item["kind"] == "report.delta" for item in events) == 1
    delta = next(item for item in events if item["kind"] == "report.delta")
    assert delta["payload"]["snapshot"]["markdown"] == markdown


def test_claim_idempotency_rejects_changed_identity(tmp_path):
    repo, runner, created, evidence, claim, *_ = _runtime(tmp_path)
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    changed = claim.model_copy(update={"text": "不同的结论"})
    with pytest.raises(RunConflict, match="deterministic verification|different identity"):
        runner.persist_verified_evidence(
            created.run["id"], lease_token=created.lease_token,
            evidence=[evidence], claims=[changed],
        )


def test_final_report_claim_citations_checkpoint_and_completion_are_atomic(tmp_path):
    repo, runner, created, evidence, claim, markdown, report_json, citations = _runtime(tmp_path)
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    runner.persist_report_snapshot(
        created.run["id"], lease_token=created.lease_token,
        generation_key="deterministic-v1", model="deterministic", schema_version=1,
        snapshot={"markdown": markdown, "report": report_json, "complete": True},
    )
    completed = runner.complete_verified_report(
        created.run["id"], lease_token=created.lease_token,
        generation_key="deterministic-v1", markdown=markdown,
        report_json=report_json, citations=citations, degraded=False,
    )
    assert completed["status"] == "completed" and completed["progress"] == 100
    events = [item["kind"] for item in repo.list_events(created.run["id"])]
    assert events[-2:] == ["report.completed", "run.completed"]
    snapshot = repo.get_runtime_snapshot(created.run["id"])
    assert snapshot["checkpoint"]["state"]["report_committed"] is True
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM report_citations").fetchone()[0] == 1


def test_unknown_citation_rolls_back_without_partial_report_or_completion(tmp_path):
    repo, runner, created, evidence, claim, markdown, report_json, citations = _runtime(tmp_path)
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    runner.persist_report_snapshot(
        created.run["id"], lease_token=created.lease_token,
        generation_key="deterministic-v1", model="deterministic", schema_version=1,
        snapshot={"markdown": markdown, "report": report_json, "complete": True},
    )
    with pytest.raises(RunConflict):
        runner.complete_verified_report(
            created.run["id"], lease_token=created.lease_token,
            generation_key="deterministic-v1", markdown=markdown,
            report_json=report_json,
            citations=[{"citation_number": 1, "claim_id": "cl1", "evidence_id": "invented"}],
            degraded=False,
        )
    assert repo.get_task(created.run["id"])["status"] == "running"
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0


def test_fake_report_with_empty_citations_cannot_complete(tmp_path):
    repo, runner, created, evidence, claim, *_ = _runtime(tmp_path)
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    fake_json = {
        "company": "腾讯", "question": "分析收入增长情况", "summary": "收入暴增99%",
        "sections": [], "limitations": [], "degraded": False, "citations": [],
    }
    fake_markdown = "# 腾讯研究报告\n\n收入暴增99%\n"
    runner.persist_report_snapshot(
        created.run["id"], lease_token=created.lease_token,
        generation_key="fake", model="deterministic", schema_version=1,
        snapshot={"markdown": fake_markdown, "report": fake_json, "complete": True},
    )
    with pytest.raises(RunConflict):
        runner.complete_verified_report(
            created.run["id"], lease_token=created.lease_token,
            generation_key="fake", markdown=fake_markdown, report_json=fake_json,
            citations=[], degraded=False,
        )
    assert repo.get_task(created.run["id"])["status"] == "running"


def test_completed_report_replay_requires_same_identity(tmp_path):
    repo, runner, created, evidence, claim, markdown, report_json, citations = _runtime(tmp_path)
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    runner.persist_report_snapshot(
        created.run["id"], lease_token=created.lease_token,
        generation_key="deterministic-v1", model="deterministic", schema_version=1,
        snapshot={"markdown": markdown, "report": report_json, "complete": True},
    )
    runner.complete_verified_report(
        created.run["id"], lease_token=created.lease_token,
        generation_key="deterministic-v1", markdown=markdown,
        report_json=report_json, citations=citations, degraded=False,
    )
    with pytest.raises(RunConflict, match="different identity"):
        runner.complete_verified_report(
            created.run["id"], lease_token=created.lease_token,
            generation_key="deterministic-v1", markdown=markdown + "伪造",
            report_json=report_json, citations=citations, degraded=False,
        )
