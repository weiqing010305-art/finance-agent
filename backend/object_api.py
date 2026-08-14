from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.models import PrincipalContext
from backend.object_store import PrivateObjectService


class UploadSlotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: str = Field(min_length=1, max_length=128)
    size: int = Field(gt=0)


def build_object_router(service: PrivateObjectService, *, can_upload, can_read) -> APIRouter:
    router = APIRouter(prefix="/api/objects", tags=["objects"])

    @router.post("/upload-slots", status_code=status.HTTP_201_CREATED)
    def create_slot(
        payload: UploadSlotRequest,
        principal: PrincipalContext = Depends(can_upload),
    ) -> dict:
        try:
            slot = service.create_upload_slot(
                principal, declared_mime=payload.content_type, declared_size=payload.size,
            )
            return {
                "object_id": slot.object_id, "upload_url": slot.upload_url,
                "upload_fields": slot.upload_fields, "expires_in": slot.expires_in,
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{object_id}/verify")
    def verify(object_id: str, principal: PrincipalContext = Depends(can_upload)) -> dict:
        try:
            return service.verify_and_promote(principal, object_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="object not found") from exc

    @router.get("/{object_id}/download")
    def download(object_id: str, principal: PrincipalContext = Depends(can_read)) -> dict[str, str]:
        try:
            return {"download_url": service.download_url(principal, object_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="object not found") from exc

    return router
