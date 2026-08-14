from pathlib import Path


SCRIPT = Path("scripts/local.ps1").read_text(encoding="utf-8")
COMPOSE = Path("compose.yaml").read_text(encoding="utf-8")


def test_local_script_generates_ignored_secrets_and_checks_docker():
    assert "RandomNumberGenerator" in SCRIPT
    assert 'Join-Path $ProjectRoot "secrets"' in SCRIPT
    assert "docker info" in SCRIPT
    assert "Docker Desktop is not running" in SCRIPT
    assert "down --volumes" not in SCRIPT
    for name in ("postgres_admin_password", "postgres_app_password", "postgres_worker_password"):
        assert name in SCRIPT
    for name in ("milvus_minio_access_key", "milvus_minio_secret_key"):
        assert name in SCRIPT and name in COMPOSE


def test_core_profile_has_durable_dispatcher_without_public_stateful_ports():
    assert "dispatcher:" in COMPOSE
    assert "scripts.reconcile_jobs" in COMPOSE
    assert "DATABASE_ROLE: finscope_worker" in COMPOSE
    for port in ("5432:5432", "6379:6379", "9000:9000"):
        assert port not in COMPOSE


def test_rag_profile_has_operational_local_only_milvus_dependencies():
    assert "milvus-etcd:" in COMPOSE
    assert "milvus-minio:" in COMPOSE
    assert "ETCD_ENDPOINTS: milvus-etcd:2379" in COMPOSE
    assert "MINIO_ADDRESS: milvus-minio:9000" in COMPOSE
    assert "condition: service_healthy" in COMPOSE
    assert "127.0.0.1:19530:19530" in COMPOSE
    assert "127.0.0.1:9091:9091" in COMPOSE
    assert "0.0.0.0:19530:19530" not in COMPOSE
    assert "milvus_etcd_data:/etcd" in COMPOSE
    assert "milvus_object_data:/minio_data" in COMPOSE
    assert "milvus_data:/var/lib/milvus" in COMPOSE


def test_caddy_csp_disallows_inline_scripts_objects_and_cross_origin_forms():
    caddy = Path("infra/caddy/Caddyfile").read_text(encoding="utf-8")
    assert "script-src 'self'" in caddy
    assert "object-src 'none'" in caddy
    assert "form-action 'self'" in caddy
    assert "frame-ancestors 'none'" in caddy
    assert "'unsafe-inline'" not in caddy
    assert "https://localhost:9443" in caddy
    assert "127.0.0.1:9443:9443" in COMPOSE


def test_bootstrap_password_uses_stdin_not_command_line_or_browser_response():
    bootstrap = Path("scripts/bootstrap_admin.py").read_text(encoding="utf-8")
    assert "--password-stdin" in bootstrap
    assert "run --rm -T migrate python scripts/container_entrypoint.py" in SCRIPT
    assert "SecureStringToBSTR" in SCRIPT and "ZeroFreeBSTR" in SCRIPT


def test_live_smoke_covers_research_and_real_presigned_object_roundtrip():
    smoke = Path("scripts/live_local_smoke.py").read_text(encoding="utf-8")
    assert "/api/research" in smoke and "/api/objects/upload-slots" in smoke
    assert "upload_fields" in smoke and "downloaded object does not match" in smoke
    assert "/api/auth/invitations" in smoke and "/api/v1/messages" in smoke
    assert "formal invitation response exposed the raw token" in smoke
    assert "/api/auth/invitations/accept" in smoke and r"invitation\.html" in smoke
