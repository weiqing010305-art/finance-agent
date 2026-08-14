from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Engine, and_, func, insert, select, text, update

from backend.auth.models import PrincipalContext, Role
from backend.auth.passwords import hash_password, verify_password
from backend.auth.tokens import TokenCodec, TokenPair, token_hash
from backend.db.metadata import invitations, memberships, refresh_tokens, tenants, users
from backend.db.session import principal_transaction


class AuthenticationError(RuntimeError):
    pass


class InvitationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InvitationToken:
    invitation_id: str
    raw_token: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if "@" not in normalized or len(normalized) > 320:
        raise ValueError("invalid email")
    return normalized


class AuthStore:
    def __init__(self, engine: Engine, codec: TokenCodec):
        self.engine = engine
        self.codec = codec

    def bootstrap_owner(self, *, email: str, password: str, tenant_name: str) -> PrincipalContext:
        email = _normalize_email(email)
        now = _utc_now()
        user_id, tenant_id = str(uuid4()), str(uuid4())
        # Bootstrap CLI uses the migration/admin connection before any tenant
        # exists; this method is never exposed by the public API.
        with self.engine.begin() as connection:
            if connection.scalar(select(func.count()).select_from(users)):
                raise AuthenticationError("bootstrap is already complete")
            connection.execute(insert(users).values(
                id=user_id, email=email, password_hash=hash_password(password), created_at=now,
            ))
            connection.execute(insert(tenants).values(id=tenant_id, name=tenant_name.strip(), created_at=now))
            connection.execute(insert(memberships).values(tenant_id=tenant_id, user_id=user_id, role="owner"))
        return PrincipalContext(user_id, tenant_id, "owner")

    def create_invitation(
        self, principal: PrincipalContext, *, email: str, role: Role, expires_at: datetime,
    ) -> InvitationToken:
        if principal.role != "owner":
            raise PermissionError("resource not found or access denied")
        if expires_at <= _utc_now():
            raise ValueError("invitation expiry must be in the future")
        raw = secrets.token_urlsafe(48)
        invitation_id = str(uuid4())
        with principal_transaction(self.engine, principal) as connection:
            membership = connection.scalar(select(memberships.c.role).where(and_(
                memberships.c.user_id == principal.user_id,
                memberships.c.tenant_id == principal.tenant_id,
            )))
            if membership != "owner":
                raise PermissionError("resource not found or access denied")
            connection.execute(insert(invitations).values(
                id=invitation_id, tenant_id=principal.tenant_id,
                email=_normalize_email(email), role=role, token_hash=token_hash(raw),
                expires_at=expires_at,
            ))
        return InvitationToken(invitation_id, raw)

    def accept_invitation(self, *, tenant_id: str, raw_token: str, password: str) -> PrincipalContext:
        now = _utc_now()
        digest = token_hash(raw_token)
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, tenant_id)
            invitation = connection.execute(
                select(invitations).where(and_(
                    invitations.c.token_hash == digest, invitations.c.tenant_id == tenant_id,
                )).with_for_update()
            ).mappings().one_or_none()
            if (
                invitation is None or invitation["accepted_at"] is not None
                or invitation["revoked_at"] is not None or _as_utc(invitation["expires_at"]) <= now
            ):
                raise InvitationError("invitation is invalid or expired")
            existing = connection.execute(
                select(users).where(users.c.email == invitation["email"])
            ).mappings().one_or_none()
            if existing is None:
                user_id = str(uuid4())
                connection.execute(insert(users).values(
                    id=user_id, email=invitation["email"], password_hash=hash_password(password),
                    created_at=now,
                ))
            else:
                user_id = existing["id"]
                if not verify_password(existing["password_hash"], password):
                    raise InvitationError("invitation is invalid or expired")
            connection.execute(insert(memberships).values(
                tenant_id=invitation["tenant_id"], user_id=user_id, role=invitation["role"],
            ))
            connection.execute(update(invitations).where(
                invitations.c.id == invitation["id"]
            ).values(accepted_at=now))
        return PrincipalContext(user_id, invitation["tenant_id"], invitation["role"])

    def revoke_invitation(self, principal: PrincipalContext, invitation_id: str) -> None:
        if principal.role != "owner":
            raise PermissionError("resource not found or access denied")
        now = _utc_now()
        with principal_transaction(self.engine, principal) as connection:
            membership = connection.scalar(select(memberships.c.role).where(and_(
                memberships.c.user_id == principal.user_id,
                memberships.c.tenant_id == principal.tenant_id,
            )))
            if membership != "owner":
                raise PermissionError("resource not found or access denied")
            result = connection.execute(update(invitations).where(and_(
                invitations.c.id == invitation_id,
                invitations.c.tenant_id == principal.tenant_id,
                invitations.c.accepted_at.is_(None), invitations.c.revoked_at.is_(None),
            )).values(revoked_at=now))
            if result.rowcount != 1:
                raise InvitationError("invitation not found")

    def login(self, *, email: str, password: str, tenant_id: str) -> TokenPair:
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, tenant_id)
            row = connection.execute(
                select(users.c.id, users.c.password_hash, users.c.is_active, memberships.c.role)
                .join(memberships, memberships.c.user_id == users.c.id)
                .where(and_(users.c.email == _normalize_email(email), memberships.c.tenant_id == tenant_id))
            ).mappings().one_or_none()
        if row is None or not row["is_active"] or not verify_password(row["password_hash"], password):
            raise AuthenticationError("invalid credentials")
        return self._issue_and_persist(PrincipalContext(row["id"], tenant_id, row["role"]))

    def revalidate_principal(self, token_principal: PrincipalContext) -> PrincipalContext:
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, token_principal.tenant_id)
            row = connection.execute(select(
                users.c.is_active, memberships.c.role,
            ).join(memberships, memberships.c.user_id == users.c.id).where(and_(
                users.c.id == token_principal.user_id,
                memberships.c.tenant_id == token_principal.tenant_id,
            ))).one_or_none()
        if row is None or not row.is_active:
            raise AuthenticationError("principal is no longer active")
        return PrincipalContext(token_principal.user_id, token_principal.tenant_id, row.role)

    def rotate_refresh(self, raw_refresh_token: str) -> TokenPair:
        now = _utc_now()
        try:
            tenant_id, _ = raw_refresh_token.split(".", 1)
        except ValueError as exc:
            raise AuthenticationError("invalid refresh token") from exc
        if not tenant_id:
            raise AuthenticationError("invalid refresh token")
        digest = token_hash(raw_refresh_token)
        replay_detected = False
        pair: TokenPair | None = None
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, tenant_id)
            row = connection.execute(select(refresh_tokens).where(
                and_(refresh_tokens.c.token_hash == digest, refresh_tokens.c.tenant_id == tenant_id)
            ).with_for_update()).mappings().one_or_none()
            if row is None:
                raise AuthenticationError("invalid refresh token")
            if row["used_at"] is not None or row["revoked_at"] is not None:
                connection.execute(update(refresh_tokens).where(
                    refresh_tokens.c.family_id == row["family_id"]
                ).values(revoked_at=now))
                replay_detected = True
            elif _as_utc(row["expires_at"]) <= now:
                raise AuthenticationError("invalid refresh token")
            else:
                role = connection.scalar(select(memberships.c.role).where(and_(
                    memberships.c.user_id == row["user_id"],
                    memberships.c.tenant_id == row["tenant_id"],
                )))
                if role is None:
                    raise AuthenticationError("invalid refresh token")
                claimed = connection.execute(update(refresh_tokens).where(and_(
                    refresh_tokens.c.id == row["id"], refresh_tokens.c.used_at.is_(None),
                    refresh_tokens.c.revoked_at.is_(None),
                )).values(used_at=now))
                if claimed.rowcount != 1:
                    connection.execute(update(refresh_tokens).where(
                        refresh_tokens.c.family_id == row["family_id"]
                    ).values(revoked_at=now))
                    replay_detected = True
                else:
                    pair = self.codec.issue(
                        PrincipalContext(row["user_id"], row["tenant_id"], role),
                        family_id=row["family_id"],
                    )
                    self._persist_pair(connection, row["user_id"], row["tenant_id"], pair)
        if replay_detected:
            raise AuthenticationError("refresh token replay detected")
        if pair is None:
            raise AuthenticationError("invalid refresh token")
        return pair

    def revoke_refresh_family(self, raw_refresh_token: str) -> bool:
        try:
            tenant_id, _ = raw_refresh_token.split(".", 1)
        except ValueError:
            return False
        digest, now = token_hash(raw_refresh_token), _utc_now()
        with self.engine.begin() as connection:
            self._set_tenant_context(connection, tenant_id)
            row = connection.execute(select(
                refresh_tokens.c.family_id,
            ).where(and_(
                refresh_tokens.c.token_hash == digest,
                refresh_tokens.c.tenant_id == tenant_id,
            )).with_for_update()).one_or_none()
            if row is None:
                return False
            connection.execute(update(refresh_tokens).where(and_(
                refresh_tokens.c.family_id == row.family_id,
                refresh_tokens.c.tenant_id == tenant_id,
            )).values(revoked_at=now))
        return True

    def _issue_and_persist(self, principal: PrincipalContext) -> TokenPair:
        pair = self.codec.issue(principal)
        with principal_transaction(self.engine, principal) as connection:
            self._persist_pair(connection, principal.user_id, principal.tenant_id, pair)
        return pair

    @staticmethod
    def _set_tenant_context(connection, tenant_id: str) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT set_config('app.user_id', 'auth-flow', true)"))
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.role', 'viewer', true)"))

    @staticmethod
    def _persist_pair(connection, user_id: str, tenant_id: str, pair: TokenPair) -> None:
        connection.execute(insert(refresh_tokens).values(
            id=str(uuid4()), user_id=user_id, tenant_id=tenant_id,
            family_id=pair.family_id, token_hash=pair.refresh_token_hash,
            expires_at=datetime.fromisoformat(pair.refresh_expires_at),
        ))
