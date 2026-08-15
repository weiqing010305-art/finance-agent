from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class SettingsError(RuntimeError):
    pass


def _read_secret(name: str, env: Mapping[str, str], *, required: bool) -> str | None:
    direct = (env.get(name) or "").strip()
    file_value = (env.get(f"{name}_FILE") or "").strip()
    if direct and file_value:
        raise SettingsError(f"configure only one of {name} or {name}_FILE")
    if file_value:
        path = Path(file_value)
        try:
            direct = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SettingsError(f"cannot read secret file for {name}") from exc
    if required and not direct:
        raise SettingsError(f"missing required secret: {name}")
    return direct or None


@dataclass(frozen=True)
class RuntimeSettings:
    mode: str
    database_url: str
    redis_url: str
    minio_endpoint: str
    minio_public_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    jwt_signing_key: str
    smtp_host: str
    smtp_port: int
    allowed_origins: tuple[str, ...]
    formal_executor: str
    milvus_uri: str
    milvus_collection: str
    rag_index_version: str
    bge_device: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeSettings":
        values = os.environ if env is None else env
        mode = (values.get("FINSCOPE_RUNTIME_MODE") or "test").strip().lower()
        if mode not in {"test", "local"}:
            raise SettingsError("FINSCOPE_RUNTIME_MODE must be test or local")
        formal = mode == "local"
        database_url = (values.get("DATABASE_URL") or "sqlite:///:memory:").strip()
        if formal and not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise SettingsError("local formal runtime requires PostgreSQL")
        redis_url = (values.get("REDIS_URL") or "redis://redis:6379/0").strip()
        minio_endpoint = (values.get("MINIO_ENDPOINT") or "http://minio:9000").strip()
        minio_public_endpoint = (
            values.get("MINIO_PUBLIC_ENDPOINT") or "https://localhost:9443"
        ).strip()
        access = _read_secret("MINIO_ACCESS_KEY", values, required=formal) or "test-access"
        secret = _read_secret("MINIO_SECRET_KEY", values, required=formal) or "test-secret"
        signing = _read_secret("JWT_SIGNING_KEY", values, required=formal) or "test-only-signing-key"
        origins = tuple(
            item.strip() for item in (values.get("ALLOWED_ORIGINS") or "https://localhost").split(",")
            if item.strip()
        )
        executor = (values.get("FINSCOPE_FORMAL_EXECUTOR") or "synthetic_smoke").strip()
        if executor not in {"synthetic_smoke", "real_rag_local"}:
            raise SettingsError("FINSCOPE_FORMAL_EXECUTOR is not supported")
        collection = (values.get("MILVUS_COLLECTION") or "finance_agent_chunks_v1").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", collection):
            raise SettingsError("MILVUS_COLLECTION is invalid")
        return cls(
            mode=mode, database_url=database_url, redis_url=redis_url,
            minio_endpoint=minio_endpoint, minio_access_key=access,
            minio_public_endpoint=minio_public_endpoint,
            minio_secret_key=secret, jwt_signing_key=signing,
            smtp_host=(values.get("SMTP_HOST") or "mailpit").strip(),
            smtp_port=int(values.get("SMTP_PORT") or "1025"),
            allowed_origins=origins,
            formal_executor=executor,
            milvus_uri=(values.get("MILVUS_URI") or "http://milvus:19530").strip(),
            milvus_collection=collection,
            rag_index_version=(values.get("RAG_INDEX_VERSION") or "formal_fixture_v1").strip(),
            bge_device=(values.get("BGE_DEVICE") or "").strip() or None,
        )
