from backend.redaction import redact_text, redact_url


def test_redact_url_removes_userinfo_and_all_common_secret_query_values():
    safe = redact_url(
        "https://urluser:urlpass@example.com/a?password=pwvalue&secret=hiddenvalue&"
        "auth=authvalue&token=tokenvalue&client_secret=clientvalue&"
        "refresh_token=refreshvalue#access_token=fragmentvalue"
    )
    for secret in (
        "urluser", "urlpass", "pwvalue", "hiddenvalue", "authvalue", "tokenvalue",
        "clientvalue", "refreshvalue", "fragmentvalue",
    ):
        assert secret not in safe
    assert safe.startswith("https://example.com/a?")
    assert "#" not in safe


def test_redact_text_consumes_complete_bearer_credential():
    safe = redact_text("request failed: Authorization: Bearer supersecret123")
    assert "supersecret123" not in safe
    assert "Authorization=[REDACTED]" in safe
