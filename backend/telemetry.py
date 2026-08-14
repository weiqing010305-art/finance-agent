from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from backend.redaction import redact_text


@dataclass
class Metrics:
    registry: CollectorRegistry
    requests: Counter
    latency: Histogram
    jobs: Gauge
    retries: Counter
    lease_loss: Counter
    quota_denied: Counter
    tool_cost: Counter


def build_metrics() -> Metrics:
    registry = CollectorRegistry()
    return Metrics(
        registry=registry,
        requests=Counter("finscope_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=registry),
        latency=Histogram("finscope_http_request_duration_seconds", "HTTP latency", ["method", "route"], registry=registry),
        jobs=Gauge("finscope_jobs", "Durable jobs", ["status"], registry=registry),
        retries=Counter("finscope_job_retries_total", "Job retries", ["kind"], registry=registry),
        lease_loss=Counter("finscope_lease_loss_total", "Lease fencing losses", ["aggregate"], registry=registry),
        quota_denied=Counter("finscope_quota_denied_total", "Quota denials", ["scope"], registry=registry),
        tool_cost=Counter("finscope_tool_cost_units_total", "Tool cost units", ["tool"], registry=registry),
    )


def configure_tracing(service_name: str, endpoint: str | None = None):
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


def instrument_fastapi(app: FastAPI, *, service_name: str = "finscope-api") -> Metrics:
    metrics = build_metrics()
    tracer = configure_tracing(service_name, os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))

    @app.middleware("http")
    async def observe(request: Request, call_next):
        started = time.perf_counter()
        route = "unmatched"
        with tracer.start_as_current_span(f"HTTP {request.method}") as span:
            span.set_attribute("http.request.method", request.method)
            response = await call_next(request)
            matched = request.scope.get("route")
            route = getattr(matched, "path", "unmatched")
            span.update_name(f"{request.method} {route}")
            span.set_attribute("http.route", route)
            span.set_attribute("http.response.status_code", response.status_code)
        metrics.requests.labels(request.method, route, str(response.status_code)).inc()
        metrics.latency.labels(request.method, route).observe(time.perf_counter() - started)
        return response

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type="text/plain; version=0.0.4")

    app.state.metrics = metrics
    return metrics


def safe_log(logger: logging.Logger, event: str, **fields) -> None:
    safe = {key: redact_text(str(value)) for key, value in fields.items() if key not in {
        "authorization", "cookie", "refresh_token", "password", "api_key",
    }}
    logger.info(event, extra={"event_fields": safe})
