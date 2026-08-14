from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Engine, and_, insert, select

from backend.auth.models import PrincipalContext
from backend.db.metadata import tenant_resources
from backend.db.session import principal_transaction


class TenantResourceStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, principal: PrincipalContext, *, kind: str, payload: dict) -> dict:
        resource_id = str(uuid4())
        created_at = datetime.now(timezone.utc)
        with principal_transaction(self.engine, principal) as connection:
            connection.execute(insert(tenant_resources).values(
                id=resource_id, tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id, kind=kind,
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                created_at=created_at,
            ))
        return {"id": resource_id, "tenant_id": principal.tenant_id, "kind": kind, "payload": payload}

    def get(self, principal: PrincipalContext, resource_id: str) -> dict | None:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(tenant_resources).where(and_(
                tenant_resources.c.id == resource_id,
                tenant_resources.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
        if row is None:
            return None
        return {
            "id": row["id"], "tenant_id": row["tenant_id"], "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
        }
