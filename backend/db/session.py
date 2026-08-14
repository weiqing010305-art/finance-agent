from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from backend.auth.models import PrincipalContext


@contextmanager
def principal_transaction(
    engine: Engine, principal: PrincipalContext
) -> Iterator[Connection]:
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT set_config('app.user_id', :value, true)"), {"value": principal.user_id})
            connection.execute(text("SELECT set_config('app.tenant_id', :value, true)"), {"value": principal.tenant_id})
            connection.execute(text("SELECT set_config('app.role', :value, true)"), {"value": principal.role})
        yield connection
