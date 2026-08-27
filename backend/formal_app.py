from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, text
import boto3
from redis import Redis

from backend.auth.api import build_auth_router
from backend.auth.dependencies import capability_dependency, principal_dependency
from backend.auth.models import PrincipalContext
from backend.auth.store import AuthStore
from backend.auth.tokens import TokenCodec
from backend.auth.email import InvitationMailer
from backend.db.resources import TenantResourceStore
from backend.db.durable import PostgresDurableRepository
from backend.db.artifacts import PostgresResearchArtifacts
from backend.formal_research_api import build_formal_research_router
from backend.jobs.ledger import JobLedger
from backend.object_api import build_object_router
from backend.object_store import PrivateObjectService, S3ObjectBackend
from backend.rate_limit import RedisSlidingWindowLimiter
from backend.settings import RuntimeSettings
from backend.schema_compat import CURRENT_ALEMBIC_REVISION
from backend.telemetry import instrument_fastapi


class LLMSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(default="deepseek", min_length=1, max_length=32)
    model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=64)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)


class ResourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=32)
    payload: dict

    @model_validator(mode="after")
    def bounded_payload(self):
        def validate_depth(value, level=0):
            if level > 8:
                raise ValueError("resource payload nesting exceeds limit")
            if isinstance(value, dict):
                for key, item in value.items():
                    if len(str(key)) > 256:
                        raise ValueError("resource payload key exceeds limit")
                    validate_depth(item, level + 1)
            elif isinstance(value, list):
                for item in value:
                    validate_depth(item, level + 1)
        validate_depth(self.payload)
        if len(json.dumps(self.payload, ensure_ascii=False).encode()) > 32 * 1024:
            raise ValueError("resource payload exceeds limit")
        return self


def create_formal_app(settings: RuntimeSettings | None = None) -> FastAPI:
    runtime = settings or RuntimeSettings.from_env()
    if runtime.mode != "local":
        raise RuntimeError("formal app requires local runtime mode")
    engine = create_engine(runtime.database_url, pool_pre_ping=True)
    codec = TokenCodec(runtime.jwt_signing_key)
    auth_store = AuthStore(engine, codec)
    resource_store = TenantResourceStore(engine)
    durable = PostgresDurableRepository(engine)
    artifacts = PostgresResearchArtifacts(engine)
    ledger = JobLedger(engine)
    s3_client = boto3.client(
        "s3", endpoint_url=runtime.minio_endpoint,
        aws_access_key_id=runtime.minio_access_key,
        aws_secret_access_key=runtime.minio_secret_key,
        region_name="us-east-1",
    )
    s3_presign_client = boto3.client(
        "s3", endpoint_url=runtime.minio_public_endpoint,
        aws_access_key_id=runtime.minio_access_key,
        aws_secret_access_key=runtime.minio_secret_key,
        region_name="us-east-1",
    )
    object_service = PrivateObjectService(
        engine, S3ObjectBackend(
            s3_client, bucket="finscope-private", presign_client=s3_presign_client,
        )
    )
    get_principal = principal_dependency(codec, auth_store.revalidate_principal)
    can_read = capability_dependency(get_principal, "resource.read")
    can_create = capability_dependency(get_principal, "research.create")
    can_upload = capability_dependency(get_principal, "document.upload")
    limiter = RedisSlidingWindowLimiter(Redis.from_url(runtime.redis_url))

    def enforce_rate(request: Request, scope: str, identity: str, *, limit: int) -> None:
        remote = request.client.host if request.client else "unknown"
        opaque_identity = hashlib.sha256(f"{remote}|{identity}".encode()).hexdigest()
        decision = limiter.check(
            scope=scope, identity=opaque_identity, limit=limit,
            window_seconds=60, fail_closed=True,
        )
        if not decision.allowed:
            raise HTTPException(status_code=429, detail="request rate limit exceeded")

    def auth_rate_guard(request: Request, scope: str, identity: str) -> None:
        enforce_rate(request, scope, identity, limit=10 if scope == "login" else 30)

    def can_create_limited(
        request: Request, principal: PrincipalContext = Depends(can_create),
    ) -> PrincipalContext:
        enforce_rate(request, "research-write", f"{principal.tenant_id}|{principal.user_id}", limit=20)
        return principal

    def can_upload_limited(
        request: Request, principal: PrincipalContext = Depends(can_upload),
    ) -> PrincipalContext:
        enforce_rate(request, "upload-write", f"{principal.tenant_id}|{principal.user_id}", limit=30)
        return principal

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if version != CURRENT_ALEMBIC_REVISION:
                raise RuntimeError("PostgreSQL schema is not at the required Alembic revision")
        try:
            s3_client.head_bucket(Bucket="finscope-private")
        except Exception as exc:
            raise RuntimeError("private object bucket is unavailable") from exc
        app.state.engine = engine
        yield
        engine.dispose()

    app = FastAPI(title="FinScope Formal API", version="0.6.0", lifespan=lifespan)
    instrument_fastapi(app)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(runtime.allowed_origins),
        allow_credentials=False, allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    app.include_router(build_auth_router(
        auth_store, rate_guard=auth_rate_guard,
        invitation_mailer=InvitationMailer(runtime.smtp_host, runtime.smtp_port),
        invitation_base_url="https://localhost:8443/invitation.html",
    ))
    app.include_router(build_object_router(
        object_service, can_upload=can_upload_limited, can_read=can_read,
    ))
    from backend.jobs.worker import execute_job
    app.include_router(build_formal_research_router(
        durable, ledger, artifacts, can_create=can_create_limited, can_read=can_read,
        sender=lambda job_id: execute_job.send(job_id),
        execution_profile=runtime.formal_executor,
    ))

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok", "runtime": "formal", "database": "postgresql",
            "research_executor": runtime.formal_executor,
        }

    @app.get("/api/securities")
    def list_securities() -> dict[str, list[dict[str, str]]]:
        """Lightweight alias catalogue for the composer's live company label.

        Mirrors the SQLite prototype endpoint; the data comes from the same
        ``backend/securities.json`` catalogue via the EntityResolver.
        """
        from backend.entity_resolver import EntityResolver
        resolver = EntityResolver()
        seen: set[tuple[str, str]] = set()
        rows: list[dict[str, str]] = []
        for entry in resolver._rows:
            key = (entry["company"], entry["symbol"])
            if key in seen:
                continue
            seen.add(key)
            for alias in entry.get("aliases", []):
                rows.append({"alias": alias, "company": entry["company"],
                             "symbol": entry["symbol"], "market": entry["market"]})
        return {"securities": rows}

    @app.get("/api/settings/llm")
    def get_llm_settings(
        principal: PrincipalContext = Depends(can_read),
    ) -> dict:
        settings = durable.get_llm_settings(principal)
        # Never send the raw key back to the browser; only a boolean flag.
        return {
            "provider": settings["provider"],
            "model": settings["model"],
            "base_url": settings["base_url"],
            "api_key_set": settings["api_key_set"],
            "source": settings["source"],
        }

    @app.put("/api/settings/llm")
    def put_llm_settings(
        payload: LLMSettingsUpdate,
        principal: PrincipalContext = Depends(can_create),
    ) -> dict:
        saved = durable.set_llm_settings(
            principal,
            provider=payload.provider,
            model=payload.model,
            api_key=payload.api_key,
            base_url=payload.base_url,
        )
        return {
            "provider": saved["provider"],
            "model": saved["model"],
            "base_url": saved["base_url"],
            "api_key_set": saved["api_key_set"],
            "source": saved["source"],
        }

    @app.post("/api/resources", status_code=status.HTTP_201_CREATED)
    def create_resource(
        payload: ResourceCreate,
        principal: PrincipalContext = Depends(can_create_limited),
    ) -> dict:
        return resource_store.create(principal, kind=payload.kind, payload=payload.payload)

    @app.get("/api/resources/{resource_id}")
    def get_resource(
        resource_id: str,
        principal: PrincipalContext = Depends(can_read),
    ) -> dict:
        result = resource_store.get(principal, resource_id)
        if result is None:
            raise HTTPException(status_code=404, detail="resource not found")
        return result

    return app
