from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.auth.models import PrincipalContext
from backend.auth.policy import Capability, require_capability
from backend.auth.tokens import TokenCodec


_bearer = HTTPBearer(auto_error=False)


def principal_dependency(
    codec: TokenCodec,
    revalidate: Callable[[PrincipalContext], PrincipalContext] | None = None,
) -> Callable[..., PrincipalContext]:
    def resolve(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> PrincipalContext:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
        try:
            principal = codec.decode_access(credentials.credentials)
            return revalidate(principal) if revalidate is not None else principal
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token") from exc
    return resolve


def capability_dependency(
    principal_resolver: Callable[..., PrincipalContext], capability: Capability,
) -> Callable[..., PrincipalContext]:
    def resolve(principal: PrincipalContext = Depends(principal_resolver)) -> PrincipalContext:
        try:
            require_capability(principal, capability)
        except (KeyError, PermissionError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="resource not found") from exc
        return principal
    return resolve
