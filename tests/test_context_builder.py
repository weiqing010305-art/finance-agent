from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.context_builder import ContextBuilder
from backend.database import Repository
from backend.durable_runner import DurableRunner
from backend.schemas import ResearchCreate


def create_run(repository: Repository, company: str = "腾讯控股") -> dict:
    return repository.create_task(
        ResearchCreate(company=company, market="HK", question=f"分析{company}盈利质量")
    )


def test_context_builder_returns_minimal_case_scoped_context(tmp_path):
    repository = Repository(tmp_path / "context.db")
    repository.initialize()
    task = create_run(repository)
    case_id = task["case_id"]
    other = create_run(repository, "小米集团")
    repository.append_conversation_turn(
        case_id, turn_id="u1", role="user", content="关注盈利质量"
    )
    repository.append_conversation_turn(
        case_id, turn_id="a1", role="assistant", content="正在分析"
    )
    repository.append_conversation_turn(
        other["case_id"], turn_id="other", role="user", content="其他 case 的秘密"
    )
    repository.replace_case_summary(case_id, "用户关注利润与现金流", last_turn_sequence=2)
    repository.put_pending_confirmation(
        case_id,
        confirmation_id="confirm-1",
        kind="security_selection",
        prompt="确认港股腾讯吗？",
        payload={"private": "must-not-enter-context"},
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )

    context = ContextBuilder(repository).build(case_id)

    assert context.case_id == case_id
    assert context.company == "腾讯控股"
    assert context.summary == "用户关注利润与现金流"
    assert [turn["content"] for turn in context.recent_turns] == ["关注盈利质量", "正在分析"]
    assert context.pending_confirmation == {
        "id": "confirm-1",
        "kind": "security_selection",
        "prompt": "确认港股腾讯吗？",
    }
    assert context.active_run["status"] == "running"
    assert set(context.active_run) == {"id", "status", "current_step", "progress"}
    assert "其他 case" not in str(context)
    assert "must-not-enter-context" not in str(context)


def test_context_builder_limits_and_redacts_recent_turns(tmp_path):
    repository = Repository(tmp_path / "limits.db")
    repository.initialize()
    case_id = create_run(repository)["case_id"]
    for index in range(12):
        repository.append_conversation_turn(
            case_id,
            turn_id=f"turn-{index}",
            role="user",
            content=(f"消息{index} token=super-secret sk-abcdefghijk " + "长" * 400),
        )

    context = ContextBuilder(repository, max_turns=4, max_chars=600).build(case_id)

    assert len(context.recent_turns) <= 4
    rendered = str(context)
    assert len("".join(item["content"] for item in context.recent_turns)) <= 600
    assert "super-secret" not in rendered
    assert "sk-abcdefghijk" not in rendered
    assert "[REDACTED]" in rendered


def test_context_builder_redacts_common_secret_formats(tmp_path):
    repository = Repository(tmp_path / "secret-formats.db")
    repository.initialize()
    case_id = create_run(repository)["case_id"]
    secrets = [
        "Authorization: Bearer supersecret123",
        '{"api_key": "secret123"}',
        "password=secret123",
        "https://example.com?a=1&token=querysecret",
        "https://urluser:urlpass@example.com/a?password=pwvalue&secret=hiddenvalue&auth=authvalue",
        "Bearer standalone-secret",
    ]
    for index, content in enumerate(secrets):
        repository.append_conversation_turn(
            case_id, turn_id=f"secret-{index}", role="user", content=content
        )

    rendered = str(ContextBuilder(repository).build(case_id))
    for secret in (
        "supersecret123", "secret123", "querysecret", "standalone-secret",
        "urluser", "urlpass", "pwvalue", "hiddenvalue", "authvalue",
    ):
        assert secret not in rendered
    assert rendered.count("[REDACTED]") >= len(secrets)


def test_context_builder_reports_completed_evidence_availability(tmp_path):
    repository = Repository(tmp_path / "report.db")
    repository.initialize()
    task = create_run(repository)
    snapshot = repository.get_runtime_snapshot(task["id"])
    DurableRunner(repository).complete_run(
        task["id"],
        lease_token=snapshot["lease"]["lease_token"],
        result={"title": "报告"},
        evidence=[{
            "citation_number": 1, "title": "公告", "publisher": "交易所",
            "url": "https://example.com", "source_type": "公告", "excerpt": "证据",
            "agent": "财报分析 Agent",
        }],
    )

    context = ContextBuilder(repository).build(task["case_id"])
    assert context.has_report is True
    assert context.report_has_evidence is True
    assert context.active_run is None


def test_context_builder_handles_no_case_and_rejects_unknown_case(tmp_path):
    repository = Repository(tmp_path / "empty.db")
    repository.initialize()
    builder = ContextBuilder(repository)

    assert builder.build(None).case_id is None
    with pytest.raises(KeyError):
        builder.build("missing")


def test_expired_confirmation_is_excluded_from_context(tmp_path):
    repository = Repository(tmp_path / "expired-context.db")
    repository.initialize()
    case_id = create_run(repository)["case_id"]
    repository.put_pending_confirmation(
        case_id,
        confirmation_id="expired",
        kind="security_selection",
        prompt="确认吗？",
        payload={},
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert ContextBuilder(repository).build(case_id).pending_confirmation is None
