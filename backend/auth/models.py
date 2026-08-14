from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Role = Literal["owner", "member", "viewer"]


@dataclass(frozen=True)
class PrincipalContext:
    user_id: str
    tenant_id: str
    role: Role

    def __post_init__(self) -> None:
        if not self.user_id or not self.tenant_id:
            raise ValueError("principal requires user_id and tenant_id")
