import pytest

from backend.settings import RuntimeSettings, SettingsError


def test_test_mode_has_explicit_non_production_defaults():
    settings = RuntimeSettings.from_env({"FINSCOPE_RUNTIME_MODE": "test"})
    assert settings.mode == "test"
    assert settings.database_url.startswith("sqlite")
    assert settings.formal_executor == "synthetic_smoke"


def test_formal_executor_is_strictly_allowlisted():
    assert RuntimeSettings.from_env({
        "FINSCOPE_RUNTIME_MODE": "test", "FINSCOPE_FORMAL_EXECUTOR": "real_rag_local",
    }).formal_executor == "real_rag_local"
    with pytest.raises(SettingsError, match="not supported"):
        RuntimeSettings.from_env({
            "FINSCOPE_RUNTIME_MODE": "test", "FINSCOPE_FORMAL_EXECUTOR": "arbitrary",
        })


def test_formal_runtime_requires_postgres_and_secrets(tmp_path):
    with pytest.raises(SettingsError, match="PostgreSQL"):
        RuntimeSettings.from_env({"FINSCOPE_RUNTIME_MODE": "local", "DATABASE_URL": "sqlite:///x.db"})
    secret = tmp_path / "jwt"
    secret.write_text("jwt-secret", encoding="utf-8")
    settings = RuntimeSettings.from_env({
        "FINSCOPE_RUNTIME_MODE": "local",
        "DATABASE_URL": "postgresql+psycopg://app@postgres/finscope",
        "MINIO_ACCESS_KEY": "access", "MINIO_SECRET_KEY": "secret",
        "JWT_SIGNING_KEY_FILE": str(secret),
    })
    assert settings.jwt_signing_key == "jwt-secret"


def test_secret_cannot_be_configured_twice(tmp_path):
    secret = tmp_path / "secret"; secret.write_text("file-value", encoding="utf-8")
    with pytest.raises(SettingsError, match="only one"):
        RuntimeSettings.from_env({
            "JWT_SIGNING_KEY": "direct", "JWT_SIGNING_KEY_FILE": str(secret)
        })
