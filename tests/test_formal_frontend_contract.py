from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = (ROOT / "prototype-research-ui/formal-console.html").read_text(encoding="utf-8")
JS = (ROOT / "prototype-research-ui/formal-console.js").read_text(encoding="utf-8")
INDEX = (ROOT / "prototype-research-ui/index.html").read_text(encoding="utf-8")
INVITATION_HTML = (ROOT / "prototype-research-ui/invitation.html").read_text(encoding="utf-8")
INVITATION_JS = (ROOT / "prototype-research-ui/invitation.js").read_text(encoding="utf-8")


def test_formal_console_is_default_and_csp_compatible():
    assert "formal-console.html" in INDEX
    assert '<script src="./formal-console.js" defer></script>' in HTML
    assert "<script>" not in HTML and "style=" not in HTML


def test_formal_console_calls_authenticated_phase6_contracts():
    for endpoint in (
        '"/auth/login"', '"/api/auth/refresh"', '"/auth/me"', '"/health"',
        '"/research"', "`/research/${state.runId}/pause`",
        "`/research/${state.runId}/resume`",
    ):
        assert endpoint in JS
    assert 'headers:{"Idempotency-Key":crypto.randomUUID()}' in JS
    assert "sessionStorage" in JS and "localStorage" not in JS
    assert "finscope.access" not in JS and "finscope.refresh" not in JS
    assert 'credentials:"same-origin"' in JS

    # Frontend reads these run fields, so GET /api/research/{run_id} must expose them.
    for field in (
        "run.company", "run.question", "run.progress", "run.state_version",
        "run.budget_used", "run.status", "run.report.markdown",
        "run.report.content_hash",
    ):
        assert field in JS, f"frontend reads {field}; backend contract must expose it"
    # POST /api/research must return run_id so the frontend can persist state.runId.
    assert "state.runId=run.run_id" in JS


def test_formal_console_shows_all_six_states_and_smoke_limitation():
    for state in ("running", "pause_requested", "paused", "resuming", "failed", "completed"):
        assert f'data-state="{state}"' in HTML
    assert "SMOKE MODE" in HTML
    assert "不调用外部金融工具" in HTML


def test_report_is_rendered_as_text_not_injected_html():
    assert '$("report").textContent=run.report.markdown' in JS
    assert "innerHTML" not in JS


def test_invitation_page_accepts_once_and_removes_token_from_address_bar_and_storage():
    assert '<script src="./invitation.js" defer></script>' in INVITATION_HTML
    assert "history.replaceState" in INVITATION_JS
    assert '"/api/auth/invitations/accept"' in INVITATION_JS
    assert 'location.replace("/formal-console.html")' in INVITATION_JS
    assert "localStorage" not in INVITATION_JS and "sessionStorage" not in INVITATION_JS
    assert "innerHTML" not in INVITATION_JS and "<script>" not in INVITATION_HTML
