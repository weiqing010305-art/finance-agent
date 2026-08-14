from __future__ import annotations

from typing import Literal

from backend.auth.models import PrincipalContext


Capability = Literal[
    "tenant.manage", "research.create", "document.upload", "memory.manage",
    "resource.read", "backup.restore", "tenant.delete",
]

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "owner": frozenset({
        "tenant.manage", "research.create", "document.upload", "memory.manage",
        "resource.read", "backup.restore", "tenant.delete",
    }),
    "member": frozenset({
        "research.create", "document.upload", "memory.manage", "resource.read",
    }),
    "viewer": frozenset({"resource.read"}),
}


def require_capability(principal: PrincipalContext, capability: Capability) -> None:
    if capability not in ROLE_CAPABILITIES[principal.role]:
        raise PermissionError("resource not found or access denied")
