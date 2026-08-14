import pytest

from backend.database import Repository
from backend.durable_runner import DurableRunner
from backend.policy import PolicyGate
from backend.schemas import PlanStep, ResearchCreate, RouteDecision
from backend.tool_registry import build_default_registry


@pytest.fixture
def setup(tmp_path):
    repository = Repository(tmp_path / "policy.db")
    repository.initialize()
    runner = DurableRunner(repository)
    created = runner.create_run(
        ResearchCreate(company="腾讯控股", symbol="0700.HK", market="HK", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="policy-run",
    )
    route = RouteDecision(
        intent="RESEARCH_NEW", confidence=1, requires_planner=True,
        external_research_allowed=False, response_policy="await_entity_resolution",
    )
    return repository, created.run, route


def test_policy_allows_registered_low_risk_tool_and_persists_decision(setup):
    repository, run, route = setup
    gate = PolicyGate(repository, build_default_registry())
    step = PlanStep(
        id="filings", tool_name="search_filings", success_criteria=["found"], estimated_cost=2
    )
    decision = gate.authorize(
        route=route, run=run, entity_confirmed=True, plan_version=2,
        step=step, budget_limit=10,
    )
    assert decision.allowed is True
    with repository.connect() as connection:
        row = connection.execute("SELECT * FROM execution_authorizations").fetchone()
    assert row["decision"] == "allow"


@pytest.mark.parametrize(
    ("entity_confirmed", "tool_name", "budget", "reason"),
    [
        (False, "search_filings", 10, "ENTITY_NOT_CONFIRMED"),
        (True, "unknown_tool", 10, "TOOL_NOT_REGISTERED"),
        (True, "search_web", 1, "BUDGET_EXCEEDED"),
    ],
)
def test_policy_fails_closed_and_persists_denial(
    tmp_path, entity_confirmed, tool_name, budget, reason
):
    repository = Repository(tmp_path / f"{reason}.db")
    repository.initialize()
    run = DurableRunner(repository).create_run(
        ResearchCreate(company="腾讯控股", symbol="0700.HK", market="HK", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key=reason,
    ).run
    route = RouteDecision(
        intent="RESEARCH_NEW", confidence=1, requires_planner=True,
        external_research_allowed=False, response_policy="await_entity_resolution",
    )
    step = PlanStep(
        id="step", tool_name=tool_name, success_criteria=["ok"], estimated_cost=3
    )
    decision = PolicyGate(repository, build_default_registry()).authorize(
        route=route, run=run, entity_confirmed=entity_confirmed,
        plan_version=2, step=step, budget_limit=budget,
    )
    assert decision.allowed is False
    assert reason in decision.reason_codes


def test_authorization_replay_cannot_change_decision(setup):
    repository, run, route = setup
    gate = PolicyGate(repository, build_default_registry())
    step = PlanStep(id="filings", tool_name="search_filings", success_criteria=["ok"])
    first = gate.authorize(
        route=route, run=run, entity_confirmed=True, plan_version=2,
        step=step, budget_limit=10,
    )
    second = gate.authorize(
        route=route, run=run, entity_confirmed=True, plan_version=2,
        step=step, budget_limit=10,
    )
    assert first == second


def test_denied_confirmation_can_be_reconsidered_and_audit_is_preserved(tmp_path):
    repository = Repository(tmp_path / "reconsider.db")
    repository.initialize()
    run = DurableRunner(repository).create_run(
        ResearchCreate(company="腾讯控股", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="reconsider",
    ).run
    route = RouteDecision(
        intent="RESEARCH_NEW", confidence=1, requires_planner=True,
        external_research_allowed=False, response_policy="await_entity_resolution",
    )
    registry = build_default_registry()
    base = registry.get("search_filings")
    registry = type(registry)()
    registry.register(
        type(base)(**{**base.__dict__, "requires_confirmation": True}),
        lambda _payload: {"status": "ok"},
    )
    gate = PolicyGate(repository, registry)
    step = PlanStep(id="filings", tool_name="search_filings", success_criteria=["ok"], estimated_cost=2)
    denied = gate.authorize(
        route=route, run=run, entity_confirmed=True, plan_version=2,
        step=step, budget_limit=10,
    )
    allowed = gate.authorize(
        route=route, run=run, entity_confirmed=True, plan_version=2,
        step=step, budget_limit=10, user_confirmed_high_risk=True,
    )
    assert denied.allowed is False and allowed.allowed is True
    assert any(event["kind"] == "authorization.reconsidered" for event in repository.list_events(run["id"]))
    with repository.connect() as connection:
        attempts = connection.execute(
            "SELECT decision FROM execution_authorization_attempts ORDER BY created_at, rowid"
        ).fetchall()
    assert [row[0] for row in attempts] == ["deny", "allow"]


def test_denied_authorization_cannot_upgrade_after_budget_is_reserved(tmp_path):
    repository = Repository(tmp_path / "reconsider-budget.db")
    repository.initialize()
    run = DurableRunner(repository).create_run(
        ResearchCreate(company="腾讯控股", question="分析腾讯盈利质量"),
        owner_id="worker", idempotency_key="reconsider-budget",
    ).run
    route = RouteDecision(
        intent="RESEARCH_NEW", confidence=1, requires_planner=True,
        external_research_allowed=False, response_policy="await_entity_resolution",
    )
    registry = build_default_registry()
    original = registry.get("search_filings")
    custom = type(registry)()
    custom.register(type(original)(**{**original.__dict__, "requires_confirmation": True}), lambda _p: {"status": "ok"})
    web = registry.get("search_web")
    custom.register(web, lambda _p: {"status": "ok"})
    gate = PolicyGate(repository, custom)
    denied_step = PlanStep(id="filings", tool_name="search_filings", success_criteria=["ok"], estimated_cost=2)
    gate.authorize(route=route, run=run, entity_confirmed=True, plan_version=2,
                   step=denied_step, budget_limit=3)
    allowed_step = PlanStep(id="web", tool_name="search_web", success_criteria=["ok"], estimated_cost=3)
    assert gate.authorize(route=route, run=run, entity_confirmed=True, plan_version=2,
                          step=allowed_step, budget_limit=3).allowed
    with pytest.raises(ValueError, match="budget reservation"):
        gate.authorize(route=route, run=run, entity_confirmed=True, plan_version=2,
                       step=denied_step, budget_limit=3, user_confirmed_high_risk=True)
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT decision, status FROM execution_authorizations WHERE step_id = 'filings'"
        ).fetchone()
        attempts = connection.execute(
            "SELECT decision FROM execution_authorization_attempts WHERE authorization_id = (SELECT id FROM execution_authorizations WHERE step_id = 'filings')"
        ).fetchall()
    assert tuple(row) == ("deny", "denied")
    assert [item[0] for item in attempts] == ["deny"]
