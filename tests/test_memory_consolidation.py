from backend.memory_consolidation import ReportMemoryConsolidator
from tests.test_report_recovery import _runtime


def test_completed_report_consolidates_supported_claim_idempotently(tmp_path):
    repo, runner, created, evidence, claim, markdown, report_json, citations = _runtime(tmp_path)
    runner.persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence], claims=[claim],
    )
    runner.persist_report_snapshot(
        created.run["id"], lease_token=created.lease_token,
        generation_key="memory-report", model="deterministic", schema_version=1,
        snapshot={"markdown": markdown, "report": report_json, "complete": True},
    )
    runner.complete_verified_report(
        created.run["id"], lease_token=created.lease_token,
        generation_key="memory-report", markdown=markdown, report_json=report_json,
        citations=citations, degraded=False,
    )
    consolidator = ReportMemoryConsolidator(repo)
    first = consolidator.consolidate(created.run["id"])
    second = consolidator.consolidate(created.run["id"])
    assert len(first) == len(second) == 1
    assert first[0].id == second[0].id
    assert first[0].status == "active"
