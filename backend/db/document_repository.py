"""Document-domain repository: a focused data-access class.

This is the first instance of the incremental de-god-classing strategy: new
code lives in domain repositories instead of growing the monolithic
``Repository``. ``DocumentRepository`` composes the shared ``Repository`` for
its connection (single migration/schema source) and adds only document-domain
queries. ``Repository`` itself stays frozen — its legacy methods remain for
existing callers and are migrated over time.
"""

from __future__ import annotations

from typing import Any

from backend.database import Repository


class DocumentRepository:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get_document_version(self, version_id: str) -> dict[str, Any] | None:
        """Read one persisted document version (including normalized text)."""
        if not version_id:
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM document_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_document_versions(
        self,
        *,
        company: str | None = None,
        market: str | None = None,
        access_scope: str = "public",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List document versions for a company, newest first.

        Documents are joined through the ``documents`` table so filtering by
        company / market / access scope stays on persisted metadata, matching
        the retrieval-time authorization boundary.
        """
        clauses = ["d.access_scope = ?"]
        parameters: list[Any] = [access_scope]
        if company:
            clauses.append("d.company = ?")
            parameters.append(company)
        if market:
            clauses.append("d.market = ?")
            parameters.append(market)
        parameters.append(min(int(limit), 50))
        with self.repository.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT dv.* FROM document_versions dv
                JOIN documents d ON d.id = dv.document_id
                WHERE {' AND '.join(clauses)}
                ORDER BY dv.created_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]
