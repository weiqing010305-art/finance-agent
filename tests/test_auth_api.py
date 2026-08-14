from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth.api import build_auth_router
from backend.auth.store import AuthStore
from backend.auth.tokens import TokenCodec
from backend.db.metadata import memberships, metadata


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    store = AuthStore(engine, TokenCodec("k" * 32))
    owner = store.bootstrap_owner(email="owner@example.com", password="correct horse battery staple", tenant_name="T")
    app = FastAPI(); app.include_router(build_auth_router(store))
    return TestClient(app, base_url="https://testserver"), store, owner


def test_login_me_invite_accept_and_viewer_rbac():
    client, _, owner = _client()
    login = client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "correct horse battery staple",
        "tenant_id": owner.tenant_id,
    })
    assert login.status_code == 200
    assert "refresh_token" not in login.json()
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    owner_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/api/auth/me", headers=owner_headers).json()["role"] == "owner"
    invited = client.post("/api/auth/invitations", headers=owner_headers, json={
        "email": "viewer@example.com", "role": "viewer",
    })
    accepted = client.post("/api/auth/invitations/accept", json={
        "tenant_id": owner.tenant_id, "token": invited.json()["token"],
        "password": "viewer correct horse battery",
    })
    viewer_headers = {"Authorization": f"Bearer {accepted.json()['access_token']}"}
    assert client.post("/api/auth/invitations", headers=viewer_headers, json={
        "email": "x@example.com", "role": "member",
    }).status_code == 404


def test_formal_mailer_receives_invitation_secret_but_http_response_does_not():
    client, store, owner = _client()
    login = client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "correct horse battery staple",
        "tenant_id": owner.tenant_id,
    })
    sent = []
    class Mailer:
        def send(self, *, recipient, invitation_url):
            sent.append((recipient, invitation_url))
    app = FastAPI()
    app.include_router(build_auth_router(store, invitation_mailer=Mailer()))
    formal = TestClient(app, base_url="https://testserver")
    response = formal.post("/api/auth/invitations", headers={
        "Authorization": f"Bearer {login.json()['access_token']}"
    }, json={"email": "mail@example.com", "role": "viewer"})
    assert response.status_code == 201 and "token" not in response.json()
    assert sent and "token=" in sent[0][1] and sent[0][0] == "mail@example.com"


def test_refresh_rotates_httponly_cookie_without_javascript_token_body():
    client, _, owner = _client()
    login = client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "correct horse battery staple",
        "tenant_id": owner.tenant_id,
    })
    before = client.cookies.get("finscope_refresh")
    refreshed = client.post("/api/auth/refresh")
    assert refreshed.status_code == 200 and "refresh_token" not in refreshed.json()
    assert client.cookies.get("finscope_refresh") != before
    active = client.cookies.get("finscope_refresh")
    assert client.post("/api/auth/logout").status_code == 204
    assert client.cookies.get("finscope_refresh") is None
    assert client.post("/api/auth/refresh", json={"refresh_token": active}).status_code == 401


def test_missing_or_invalid_bearer_is_401_without_token_details():
    client, _, _ = _client()
    assert client.get("/api/auth/me").status_code == 401
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401 and response.json() == {"detail": "invalid access token"}


def test_auth_rate_guard_runs_before_password_verification():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    store = AuthStore(engine, TokenCodec("k" * 32))
    owner = store.bootstrap_owner(
        email="owner@example.com", password="correct horse battery staple", tenant_name="T",
    )
    seen = []
    app = FastAPI()
    app.include_router(build_auth_router(
        store, rate_guard=lambda request, scope, identity: seen.append((scope, identity)),
    ))
    client = TestClient(app, base_url="https://testserver")
    response = client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "correct horse battery staple",
        "tenant_id": owner.tenant_id,
    })
    assert response.status_code == 200
    assert seen == [("login", f"{owner.tenant_id}|owner@example.com")]


def test_existing_access_token_is_rejected_immediately_after_membership_removal():
    client, store, owner = _client()
    login = client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "correct horse battery staple",
        "tenant_id": owner.tenant_id,
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    with store.engine.begin() as connection:
        connection.execute(memberships.delete().where(
            memberships.c.tenant_id == owner.tenant_id
        ))
    assert client.get("/api/auth/me", headers=headers).status_code == 401
