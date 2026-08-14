from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from backend.database import Repository
from backend.memory import MemoryService
from backend.schemas import MemoryCandidate, MemoryScope
from backend.durable_runner import DurableRunner
from backend.evidence import EvidenceBuilder
from backend.schemas import VerifiedClaim
from tests.test_memory_lifecycle import _company_runtime, _company_candidate


def _preference(value: str, key: str) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="user_preference", memory_key="report_style",
        scope=MemoryScope(scope_kind="user", tenant_id="local", user_id="default"),
        content={"style": value}, content_text=f"Prefer {value} reports",
        idempotency_key=key, confidence=1, explicit_user_confirmation=True,
    )


def test_explicit_preference_correction_supersedes_previous_version(tmp_path):
    repo = Repository(tmp_path / "preference-conflict.db")
    repo.initialize()
    service = MemoryService(repo)
    first = service.remember(_preference("concise", "pref-1"))
    second = service.remember(_preference("detailed", "pref-2"))
    versions = repo.list_memory_versions(first.memory_id)
    assert [(item.id, item.status) for item in versions] == [
        (first.id, "superseded"), (second.id, "active")
    ]


def test_identical_content_merges_without_creating_a_new_version(tmp_path):
    repo = Repository(tmp_path / "preference-merge.db")
    repo.initialize()
    service = MemoryService(repo)
    first = service.remember(_preference("concise", "merge-1"))
    replay = service.remember(_preference("concise", "merge-2"))
    assert replay.id == first.id
    assert len(repo.list_memory_versions(first.memory_id)) == 1


def test_concurrent_corrections_leave_exactly_one_active_version(tmp_path):
    repo = Repository(tmp_path / "preference-race.db")
    repo.initialize()
    service = MemoryService(repo)
    first = service.remember(_preference("concise", "race-base"))
    candidates = [_preference("detailed", "race-a"), _preference("bullet", "race-b")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.remember, candidates))
    versions = repo.list_memory_versions(first.memory_id)
    assert sum(item.status == "active" for item in versions) == 1
    assert len(versions) == 3
    assert {item.id for item in results} <= {item.id for item in versions}


def test_company_conflict_gives_both_versions_seven_day_ttl_and_expires(tmp_path):
    from datetime import datetime, timedelta, timezone

    repo, created, evidence, claim = _company_runtime(tmp_path)
    service = MemoryService(repo)
    first = service.remember(_company_candidate(created, evidence, claim, key="conflict-a"))
    evidence_b = EvidenceBuilder().build_retrieval_items(created.run["id"], [{
        "text": "Tencent 2024 revenue increased 6%.",
        "source_uri": "https://example.com/filing-2024-b", "title": "2024 filing B",
        "publisher": "HKEX", "authority_tier": 5, "company": "Tencent", "period": "2024",
    }])[0]
    claim_b = VerifiedClaim(
        id="claim-revenue-2024-b", run_id=created.run["id"],
        text="Tencent 2024 revenue increased 6%.", status="supported", confidence=.9,
        evidence_ids=[evidence_b.id], reason_codes=[], period="2024", unit="%",
    )
    DurableRunner(repo).persist_verified_evidence(
        created.run["id"], lease_token=created.lease_token,
        evidence=[evidence_b], claims=[claim_b],
    )
    second_candidate = _company_candidate(
        created, evidence_b, claim_b, key="conflict-b"
    ).model_copy(update={
        "content": {"metric": "revenue_growth", "value": 6, "period": "2024"}
    })
    second = service.remember(second_candidate)
    versions = repo.list_memory_versions(first.memory_id)
    assert [item.status for item in versions] == ["conflicted", "conflicted"]
    expiries = [datetime.fromisoformat(item.expires_at) for item in versions]
    assert all(timedelta(days=6, hours=23) < item - datetime.now(timezone.utc) <= timedelta(days=7) for item in expiries)
    assert repo.expire_memory_versions(
        now=(datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
    ) == 2
    assert [item.status for item in repo.list_memory_versions(second.memory_id)] == ["expired", "expired"]
