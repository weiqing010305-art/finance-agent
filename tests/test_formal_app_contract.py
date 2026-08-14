import pytest

from backend.formal_app import create_formal_app
from backend.settings import RuntimeSettings, SettingsError


def test_formal_app_rejects_test_runtime():
    settings = RuntimeSettings.from_env({})
    with pytest.raises(RuntimeError, match="formal app requires"):
        create_formal_app(settings)


def test_local_runtime_rejects_sqlite_before_engine_creation():
    with pytest.raises(SettingsError, match="PostgreSQL"):
        RuntimeSettings.from_env({
            "FINSCOPE_RUNTIME_MODE": "local", "DATABASE_URL": "sqlite:///bad.db",
            "MINIO_ACCESS_KEY": "x", "MINIO_SECRET_KEY": "y", "JWT_SIGNING_KEY": "z" * 32,
        })
