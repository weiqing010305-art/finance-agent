from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from backend.auth.store import AuthenticationError, AuthStore, InvitationError
from backend.auth.tokens import TokenCodec
from backend.db.metadata import metadata, refresh_tokens


@pytest.fixture()
def auth_store():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    metadata.create_all(engine)
    return AuthStore(engine, TokenCodec("a" * 32)), engine


def test_bootstrap_invite_accept_and_login(auth_store):
    store, _ = auth_store
    owner = store.bootstrap_owner(
        email="OWNER@example.com", password="correct horse battery staple", tenant_name="Personal",
    )
    invitation = store.create_invitation(
        owner, email="Member@example.com", role="member",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    member = store.accept_invitation(
        tenant_id=owner.tenant_id, raw_token=invitation.raw_token, password="another correct horse battery",
    )
    pair = store.login(
        email="member@example.com", password="another correct horse battery",
        tenant_id=owner.tenant_id,
    )
    assert member.role == "member"
    assert store.codec.decode_access(pair.access_token) == member


def test_invitation_token_is_single_use(auth_store):
    store, _ = auth_store
    owner = store.bootstrap_owner(email="owner@x.test", password="correct horse battery staple", tenant_name="T")
    invitation = store.create_invitation(
        owner, email="member@x.test", role="viewer",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    store.accept_invitation(tenant_id=owner.tenant_id, raw_token=invitation.raw_token, password="another correct horse battery")
    with pytest.raises(InvitationError):
        store.accept_invitation(tenant_id=owner.tenant_id, raw_token=invitation.raw_token, password="another correct horse battery")


def test_invitation_cannot_be_routed_through_another_tenant(auth_store):
    store, _ = auth_store
    owner = store.bootstrap_owner(email="owner@x.test", password="correct horse battery staple", tenant_name="T")
    invitation = store.create_invitation(
        owner, email="member@x.test", role="member",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with pytest.raises(InvitationError):
        store.accept_invitation(
            tenant_id="forged-tenant", raw_token=invitation.raw_token,
            password="another correct horse battery",
        )


def test_refresh_rotation_replay_revokes_entire_family(auth_store):
    store, engine = auth_store
    owner = store.bootstrap_owner(email="owner@x.test", password="correct horse battery staple", tenant_name="T")
    first = store.login(email="owner@x.test", password="correct horse battery staple", tenant_id=owner.tenant_id)
    second = store.rotate_refresh(first.refresh_token)
    with pytest.raises(AuthenticationError, match="replay"):
        store.rotate_refresh(first.refresh_token)
    with pytest.raises(AuthenticationError, match="replay"):
        store.rotate_refresh(second.refresh_token)
    with engine.connect() as connection:
        rows = connection.execute(select(refresh_tokens.c.revoked_at)).all()
    assert rows and all(value is not None for (value,) in rows)


def test_access_key_rotation_keeps_previous_verification_key():
    old = TokenCodec("o" * 32, active_kid="v1")
    store_principal = __import__("backend.auth.models", fromlist=["PrincipalContext"]).PrincipalContext(
        "u", "t", "viewer"
    )
    token = old.issue(store_principal).access_token
    rotated = TokenCodec("n" * 32, active_kid="v2", verification_keys={"v1": "o" * 32})
    assert rotated.decode_access(token) == store_principal
