import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.telemetry import instrument_fastapi, safe_log


def test_metrics_export_route_and_status_without_authorization_value():
    app = FastAPI(); instrument_fastapi(app, service_name="test")
    @app.get("/hello")
    def hello(): return {"ok": True}
    with TestClient(app) as client:
        client.get("/hello", headers={"Authorization": "Bearer super-secret"})
        body = client.get("/metrics").text
    assert "finscope_http_requests_total" in body and "/hello" in body
    assert "super-secret" not in body


def test_safe_log_drops_secret_fields_and_redacts_text(caplog):
    logger = logging.getLogger("telemetry-test")
    with caplog.at_level(logging.INFO):
        safe_log(logger, "tool.failed", authorization="Bearer secret", detail="api_key=secret")
    assert "Bearer secret" not in caplog.text and "api_key=secret" not in caplog.text


def test_metrics_use_route_templates_not_resource_ids():
    app = FastAPI(); instrument_fastapi(app, service_name="route-template-test")
    @app.get("/runs/{run_id}")
    def run(run_id: str): return {"id": run_id}
    with TestClient(app) as client:
        client.get("/runs/private-resource-123")
        body = client.get("/metrics").text
    assert "/runs/{run_id}" in body
    assert "private-resource-123" not in body
