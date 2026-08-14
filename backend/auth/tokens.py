from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from backend.auth.models import PrincipalContext


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    refresh_token_hash: str
    family_id: str
    refresh_expires_at: str


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TokenCodec:
    def __init__(
        self, signing_key: str, *, issuer: str = "finscope-local", audience: str = "finscope-api",
        active_kid: str = "local-v1", verification_keys: dict[str, str] | None = None,
    ):
        if len(signing_key.encode("utf-8")) < 32:
            raise ValueError("signing key is too short")
        self.signing_key = signing_key
        self.issuer = issuer
        self.audience = audience
        self.active_kid = active_kid
        self.verification_keys = dict(verification_keys or {})
        self.verification_keys[active_kid] = signing_key

    def issue(self, principal: PrincipalContext, *, family_id: str | None = None) -> TokenPair:
        now = datetime.now(timezone.utc)
        access_expiry = now + timedelta(minutes=15)
        refresh_expiry = now + timedelta(days=7)
        family = family_id or secrets.token_urlsafe(24)
        # Tenant prefix is non-secret routing metadata. It lets the refresh path
        # establish PostgreSQL RLS context before looking up the hashed token.
        refresh = f"{principal.tenant_id}.{secrets.token_urlsafe(48)}"
        access = jwt.encode({
            "sub": principal.user_id, "tenant_id": principal.tenant_id,
            "role": principal.role, "iss": self.issuer, "aud": self.audience, "iat": now,
            "exp": access_expiry, "type": "access",
        }, self.signing_key, algorithm="HS256", headers={"kid": self.active_kid})
        return TokenPair(
            access_token=access, refresh_token=refresh,
            refresh_token_hash=token_hash(refresh), family_id=family,
            refresh_expires_at=refresh_expiry.isoformat(),
        )

    def decode_access(self, value: str) -> PrincipalContext:
        header = jwt.get_unverified_header(value)
        kid = header.get("kid")
        key = self.verification_keys.get(kid)
        if key is None:
            raise ValueError("unknown signing key")
        payload = jwt.decode(
            value, key, algorithms=["HS256"], issuer=self.issuer, audience=self.audience,
            options={"require": ["sub", "tenant_id", "role", "exp", "type", "aud"]},
        )
        if payload["type"] != "access":
            raise ValueError("wrong token type")
        return PrincipalContext(
            user_id=payload["sub"], tenant_id=payload["tenant_id"], role=payload["role"]
        )
