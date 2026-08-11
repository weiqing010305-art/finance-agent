from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.database import Repository
from backend.schemas import ResearchCreate


def create_case(repository: Repository, company: str = "腾讯控股") -> str:
    task = repository.create_task(
        ResearchCreate(company=company, market="HK", question=f"分析{company}盈利质量")
    )
    return task["case_id"]


def test_turns_are_sequenced_idempotent_and_case_isolated(tmp_path):
    repository = Repository(tmp_path / "turns.db")
    repository.initialize()
    first_case = create_case(repository)
    second_case = create_case(repository, "小米集团")

    first = repository.append_conversation_turn(
        first_case, turn_id="turn-1", role="user", content="再看看现金流"
    )
    duplicate = repository.append_conversation_turn(
        first_case, turn_id="turn-1", role="user", content="再看看现金流"
    )
    second = repository.append_conversation_turn(
        first_case,
        turn_id="turn-2",
        role="assistant",
        content="我会继续分析现金流。",
        intent="RESEARCH_FOLLOWUP",
        reason_codes=["ACTIVE_CASE", "EXPLICIT_ANALYSIS_VERB"],
    )
    repository.append_conversation_turn(
        second_case, turn_id="other-1", role="user", content="好的"
    )

    assert first == duplicate
    assert [item["sequence"] for item in repository.list_conversation_turns(first_case)] == [1, 2]
    assert second["reason_codes"] == ["ACTIVE_CASE", "EXPLICIT_ANALYSIS_VERB"]
    assert len(repository.list_conversation_turns(second_case)) == 1

    with pytest.raises(ValueError, match="different content"):
        repository.append_conversation_turn(
            first_case, turn_id="turn-1", role="user", content="改成研究利润"
        )
    with pytest.raises(KeyError):
        repository.append_conversation_turn(
            "missing-case", turn_id="missing", role="user", content="hello"
        )


def test_summary_versions_and_confirmation_lifecycle(tmp_path):
    repository = Repository(tmp_path / "memory.db")
    repository.initialize()
    case_id = create_case(repository)

    repository.append_conversation_turn(case_id, turn_id="s1", role="user", content="盈利质量")
    repository.append_conversation_turn(case_id, turn_id="s2", role="user", content="现金流")
    first = repository.replace_case_summary(case_id, "用户关注盈利质量", last_turn_sequence=1)
    second = repository.replace_case_summary(case_id, "用户追加关注现金流", last_turn_sequence=2)
    assert first["version"] == 1
    assert second["version"] == 2
    assert repository.get_case_summary(case_id)["summary"] == "用户追加关注现金流"

    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    confirmation = repository.put_pending_confirmation(
        case_id,
        confirmation_id="confirm-1",
        kind="security_selection",
        prompt="是港股腾讯控股吗？",
        payload={"candidates": ["0700.HK"]},
        expires_at=future,
    )
    assert repository.get_pending_confirmation(case_id)["id"] == confirmation["id"]
    resolved = repository.resolve_pending_confirmation(
        case_id, confirmation_id="confirm-1", value={"accepted": True}
    )
    assert resolved["status"] == "resolved"
    assert resolved["resolved_value"] == {"accepted": True}
    assert repository.get_pending_confirmation(case_id) is None


def test_expired_confirmation_is_not_returned(tmp_path):
    repository = Repository(tmp_path / "expired.db")
    repository.initialize()
    case_id = create_case(repository)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    repository.put_pending_confirmation(
        case_id,
        confirmation_id="expired",
        kind="security_selection",
        prompt="确认吗？",
        payload={},
        expires_at=past,
    )
    assert repository.get_pending_confirmation(case_id) is None


def test_concurrent_turns_receive_unique_sequences(tmp_path):
    repository = Repository(tmp_path / "concurrent-turns.db")
    repository.initialize()
    case_id = create_case(repository)

    def append(index: int):
        return repository.append_conversation_turn(
            case_id, turn_id=f"turn-{index}", role="user", content=f"消息 {index}"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(12)))

    turns = repository.list_conversation_turns(case_id)
    assert [item["sequence"] for item in turns] == list(range(1, 13))


def test_confirmation_expiry_is_timezone_safe_and_audited(tmp_path):
    repository = Repository(tmp_path / "timezone-expiry.db")
    repository.initialize()
    case_id = create_case(repository)
    expired_with_offset = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).astimezone(timezone(timedelta(hours=8))).isoformat()

    stored = repository.put_pending_confirmation(
        case_id,
        confirmation_id="offset-expired",
        kind="security_selection",
        prompt="确认吗？",
        payload={},
        expires_at=expired_with_offset,
    )
    assert stored["status"] == "expired"
    assert stored["expires_at"].endswith("+00:00")
    assert repository.get_pending_confirmation(case_id) is None


def test_only_one_concurrent_confirmation_resolve_succeeds(tmp_path):
    repository = Repository(tmp_path / "confirmation-race.db")
    repository.initialize()
    case_id = create_case(repository)
    repository.put_pending_confirmation(
        case_id,
        confirmation_id="race",
        kind="security_selection",
        prompt="确认吗？",
        payload={},
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )

    def resolve(value: bool):
        try:
            return repository.resolve_pending_confirmation(
                case_id, confirmation_id="race", value={"accepted": value}
            )["status"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(resolve, (True, False)))
    assert sorted(outcomes) == ["conflict", "resolved"]


def test_summary_cursor_must_follow_persisted_turns_monotonically(tmp_path):
    repository = Repository(tmp_path / "summary-cursor.db")
    repository.initialize()
    case_id = create_case(repository)
    repository.append_conversation_turn(case_id, turn_id="one", role="user", content="第一条")
    repository.replace_case_summary(case_id, "摘要一", last_turn_sequence=1)

    with pytest.raises(ValueError, match="last_turn_sequence"):
        repository.replace_case_summary(case_id, "未来摘要", last_turn_sequence=2)
    with pytest.raises(ValueError, match="last_turn_sequence"):
        repository.replace_case_summary(case_id, "倒退摘要", last_turn_sequence=0)


def test_route_result_ledger_replays_one_concurrent_winner(tmp_path):
    repository = Repository(tmp_path / "route-ledger.db")
    repository.initialize()

    def save(label: str):
        return repository.save_route_request_result(
            "route-1",
            case_id=None,
            message="好的",
            decision={
                "intent": "SOCIAL_ACK",
                "confidence": 0.99,
                "case_id": None,
                "requires_planner": False,
                "external_research_allowed": False,
                "response_policy": "template_reply",
                "reason_codes": [label],
            },
            response=f"response-{label}",
            trace=[label],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ("a", "b")))
    assert results[0] == results[1]
    assert repository.get_route_request("route-1") == results[0]

    with pytest.raises(ValueError, match="different route request"):
        repository.save_route_request_result(
            "route-1",
            case_id=None,
            message="研究腾讯",
            decision=results[0]["decision"],
            response="changed",
            trace=[],
        )


def test_v5_confirmation_expiry_is_normalized_during_v6_upgrade(tmp_path):
    repository = Repository(tmp_path / "v5-confirmation.db")
    repository.initialize()
    case_id = create_case(repository)
    naive_case_id = create_case(repository, "小米集团")
    expired_with_offset = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).astimezone(timezone(timedelta(hours=8))).isoformat()
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_authorizations")
        connection.execute("DROP TABLE entity_confirmations")
        connection.execute("DROP TABLE research_intakes")
        connection.execute("DROP TABLE route_requests")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 6")
        connection.execute(
            """
            INSERT INTO pending_confirmations(
                id, case_id, kind, prompt, payload_json, status, expires_at,
                resolved_value_json, created_at, updated_at
            ) VALUES ('legacy-offset', ?, 'security', '确认吗', '{}', 'pending', ?, NULL, 't', 't')
            """,
            (case_id, expired_with_offset),
        )
        connection.execute(
            """
            INSERT INTO pending_confirmations(
                id, case_id, kind, prompt, payload_json, status, expires_at,
                resolved_value_json, created_at, updated_at
            ) VALUES ('legacy-naive', ?, 'security', '确认吗', '{}', 'pending',
                      '2026-08-11T12:00:00', NULL, 't', 't')
            """,
            (naive_case_id,),
        )

    repository.initialize()

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT status, expires_at FROM pending_confirmations WHERE id = 'legacy-offset'"
        ).fetchone()
    assert row["status"] == "expired"
    assert row["expires_at"].endswith("+00:00")
    with repository.connect() as connection:
        naive = connection.execute(
            "SELECT status, expires_at FROM pending_confirmations WHERE id = 'legacy-naive'"
        ).fetchone()
    assert naive["status"] == "expired"
    assert naive["expires_at"].endswith("+00:00")
    assert repository.get_pending_confirmation(case_id) is None


def test_v6_upgrade_preserves_audit_timestamp_when_confirmation_is_already_canonical(tmp_path):
    repository = Repository(tmp_path / "v5-canonical-confirmation.db")
    repository.initialize()
    case_id = create_case(repository)
    with repository.connect() as connection:
        connection.execute("DROP TABLE execution_authorizations")
        connection.execute("DROP TABLE entity_confirmations")
        connection.execute("DROP TABLE research_intakes")
        connection.execute("DROP TABLE route_requests")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 6")
        connection.execute(
            """
            INSERT INTO pending_confirmations(
                id, case_id, kind, prompt, payload_json, status, expires_at,
                resolved_value_json, created_at, updated_at
            ) VALUES ('legacy-resolved', ?, 'security', 'done', '{}', 'resolved',
                      '2026-08-11T12:00:00+00:00', '{}', 'original', 'original')
            """,
            (case_id,),
        )

    repository.initialize()

    with repository.connect() as connection:
        row = connection.execute(
            "SELECT status, expires_at, updated_at FROM pending_confirmations "
            "WHERE id = 'legacy-resolved'"
        ).fetchone()
    assert dict(row) == {
        "status": "resolved",
        "expires_at": "2026-08-11T12:00:00+00:00",
        "updated_at": "original",
    }
