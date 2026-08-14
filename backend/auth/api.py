from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Callable
import hashlib
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from backend.auth.dependencies import capability_dependency, principal_dependency
from backend.auth.models import PrincipalContext
from backend.auth.store import AuthenticationError, AuthStore, InvitationError
from backend.auth.email import InvitationMailer


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=64)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=32, max_length=512)


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role: Literal["member", "viewer"]
    expires_in_hours: int = Field(default=24, ge=1, le=168)


class AcceptInvitationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = 900


def _token_response(pair, response: Response) -> TokenResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.set_cookie(
        "finscope_refresh", pair.refresh_token, max_age=7 * 24 * 60 * 60,
        httponly=True, secure=True, samesite="strict", path="/api/auth",
    )
    return TokenResponse(access_token=pair.access_token)


RateGuard = Callable[[Request, str, str], None]


def build_auth_router(
    store: AuthStore, *, rate_guard: RateGuard | None = None,
    invitation_mailer: InvitationMailer | None = None,
    invitation_base_url: str = "https://localhost:8443/invitation.html",
) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])
    get_principal = principal_dependency(store.codec, store.revalidate_principal)
    require_tenant_manage = capability_dependency(get_principal, "tenant.manage")

    @router.post("/login", response_model=TokenResponse)
    def login(payload: LoginRequest, request: Request, response: Response) -> TokenResponse:
        if rate_guard is not None:
            rate_guard(request, "login", f"{payload.tenant_id}|{str(payload.email).casefold()}")
        try:
            return _token_response(store.login(
                email=str(payload.email), password=payload.password, tenant_id=payload.tenant_id,
            ), response)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials") from exc

    @router.post("/refresh", response_model=TokenResponse)
    def refresh(request: Request, response: Response, payload: RefreshRequest | None = None) -> TokenResponse:
        raw_refresh = payload.refresh_token if payload is not None else request.cookies.get("finscope_refresh")
        if rate_guard is not None:
            identity = hashlib.sha256(raw_refresh.encode()).hexdigest()[:24] if raw_refresh else "missing-cookie"
            rate_guard(request, "refresh", identity)
        if not raw_refresh:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token")
        try:
            return _token_response(store.rotate_refresh(raw_refresh), response)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token") from exc

    @router.post("/invitations", status_code=status.HTTP_201_CREATED)
    def invite(
        payload: InviteRequest,
        principal: PrincipalContext = Depends(require_tenant_manage),
    ) -> dict[str, str]:
        invitation = store.create_invitation(
            principal, email=str(payload.email), role=payload.role,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours),
        )
        if invitation_mailer is not None:
            query = urlencode({"tenant_id": principal.tenant_id, "token": invitation.raw_token})
            invitation_mailer.send(
                recipient=str(payload.email), invitation_url=f"{invitation_base_url}?{query}",
            )
            return {"invitation_id": invitation.invitation_id}
        # Deterministic unit-test adapter only; the formal runtime always supplies Mailpit.
        return {"invitation_id": invitation.invitation_id, "token": invitation.raw_token}

    @router.post("/invitations/accept", response_model=TokenResponse)
    def accept(payload: AcceptInvitationRequest, request: Request, response: Response) -> TokenResponse:
        if rate_guard is not None:
            rate_guard(request, "invitation-accept", payload.tenant_id)
        try:
            principal = store.accept_invitation(
                tenant_id=payload.tenant_id, raw_token=payload.token, password=payload.password,
            )
            return _token_response(store._issue_and_persist(principal), response)
        except InvitationError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="invitation is invalid") from exc

    @router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
    def revoke(
        invitation_id: str,
        principal: PrincipalContext = Depends(require_tenant_manage),
    ) -> None:
        try:
            store.revoke_invitation(principal, invitation_id)
        except InvitationError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="resource not found") from exc

    @router.get("/me")
    def me(principal: PrincipalContext = Depends(get_principal)) -> dict[str, str]:
        return {"user_id": principal.user_id, "tenant_id": principal.tenant_id, "role": principal.role}

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request, response: Response) -> None:
        raw_refresh = request.cookies.get("finscope_refresh")
        if raw_refresh:
            store.revoke_refresh_family(raw_refresh)
        response.delete_cookie("finscope_refresh", path="/api/auth", secure=True, httponly=True, samesite="strict")

    return router
