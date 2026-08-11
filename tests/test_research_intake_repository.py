from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from backend.database import Repository
from backend.entity_resolver import EntityResolver


def save_route(repository: Repository, request_id: str, message: str, *, research: bool = True):
    intent = "RESEARCH_NEW" if research else "SOCIAL_ACK"
    return repository.save_route_request_result(
        request_id,
        case_id=None,
        message=message,
        decision={
            "intent": intent,
            "confidence": 1.0,
            "case_id": None,
            "requires_planner": research,
            "external_research_allowed": False,
            "response_policy": "await_entity_resolution" if research else "template_reply",
            "reason_codes": [],
        },
        response="ok",
        trace=[],
    )


def test_resolved_intake_is_idempotent_and_payload_conflicts(tmp_path):
    repository = Repository(tmp_path / "intake.db")
    repository.initialize()
    save_route(repository, "route-1", "研究腾讯")
    resolution = EntityResolver().resolve("研究腾讯").model_dump()

    first = repository.create_research_intake(
        "route-1", depth="standard", budget_limit=20, resolution=resolution
    )
    replay = repository.create_research_intake(
        "route-1", depth="standard", budget_limit=20, resolution=resolution
    )

    assert first == replay
    assert first["status"] == "ready"
    assert first["resolved_entity"]["symbol"] == "0700.HK"
    with pytest.raises(ValueError, match="different intake"):
        repository.create_research_intake(
            "route-1", depth="deep", budget_limit=20, resolution=resolution
        )


def test_non_research_route_cannot_create_intake(tmp_path):
    repository = Repository(tmp_path / "denied.db")
    repository.initialize()
    save_route(repository, "route-social", "谢谢", research=False)
    resolution = EntityResolver().resolve("研究腾讯").model_dump()
    with pytest.raises(PermissionError):
        repository.create_research_intake(
            "route-social", depth="standard", budget_limit=20, resolution=resolution
        )


def test_ambiguous_intake_requires_and_resolves_confirmation(tmp_path):
    repository = Repository(tmp_path / "confirmation.db")
    repository.initialize()
    save_route(repository, "route-byd", "研究比亚迪")
    resolution = EntityResolver().resolve("研究比亚迪").model_dump()
    intake = repository.create_research_intake(
        "route-byd",
        depth="standard",
        budget_limit=20,
        resolution=resolution,
        confirmation_id="confirm-byd",
        confirmation_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    selected_id = resolution["candidates"][0]["candidate_id"]

    resolved = repository.resolve_entity_confirmation(
        intake["id"], candidate_id=selected_id
    )
    replay = repository.resolve_entity_confirmation(
        intake["id"], candidate_id=selected_id
    )

    assert resolved == replay
    assert resolved["status"] == "ready"
    assert resolved["resolved_entity"]["candidate_id"] == selected_id
    with pytest.raises(ValueError, match="already resolved differently"):
        repository.resolve_entity_confirmation(
            intake["id"], candidate_id=resolution["candidates"][1]["candidate_id"]
        )


def test_expired_confirmation_fails_closed_and_updates_audit_state(tmp_path):
    repository = Repository(tmp_path / "expired-intake.db")
    repository.initialize()
    save_route(repository, "route-ali", "研究阿里巴巴")
    resolution = EntityResolver().resolve("研究阿里巴巴").model_dump()
    intake = repository.create_research_intake(
        "route-ali",
        depth="quick",
        budget_limit=10,
        resolution=resolution,
        confirmation_id="confirm-ali",
        confirmation_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ValueError, match="expired"):
        repository.resolve_entity_confirmation(
            intake["id"], candidate_id=resolution["candidates"][0]["candidate_id"]
        )
    assert repository.get_research_intake(intake["id"])["status"] == "needs_clarification"


def test_concurrent_different_confirmations_have_one_winner(tmp_path):
    repository = Repository(tmp_path / "confirmation-race.db")
    repository.initialize()
    save_route(repository, "route-race", "研究比亚迪")
    resolution = EntityResolver().resolve("研究比亚迪").model_dump()
    intake = repository.create_research_intake(
        "route-race",
        depth="standard",
        budget_limit=20,
        resolution=resolution,
        confirmation_id="confirm-race",
        confirmation_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )

    def choose(candidate_id):
        try:
            return repository.resolve_entity_confirmation(intake["id"], candidate_id=candidate_id)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(choose, [item["candidate_id"] for item in resolution["candidates"]]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, str) for item in results) == 1
