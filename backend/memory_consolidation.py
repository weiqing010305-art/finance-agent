from __future__ import annotations

from backend.database import Repository
from backend.memory import MemoryService
from backend.schemas import MemoryCandidate, MemoryScope


class ReportMemoryConsolidator:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.memory = MemoryService(repository)

    def consolidate(self, run_id: str) -> list:
        with self.repository.connect() as connection:
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id=? AND status='completed'", (run_id,)
            ).fetchone()
            report = connection.execute("SELECT id FROM reports WHERE run_id=?", (run_id,)).fetchone()
            if run is None or report is None:
                raise ValueError("memory consolidation requires a completed persisted report")
            rows = connection.execute(
                """
                SELECT c.*,e.id evidence_id,e.company evidence_company
                FROM report_citations rc JOIN claims c ON c.id=rc.claim_id
                JOIN evidence_items e ON e.id=rc.evidence_id
                WHERE rc.report_id=? AND c.status='supported' AND e.access_scope='public'
                ORDER BY rc.citation_number
                """,
                (report["id"],),
            ).fetchall()
        results = []
        for row in rows:
            results.append(self.memory.remember(MemoryCandidate(
                memory_type="company_fact",
                memory_key=f"claim:{row['content_sha256']}:{row['period'] or 'current'}",
                scope=MemoryScope(
                    scope_kind="public_company", tenant_id="public", company=run["company"],
                    symbol=run["symbol"], market=run["market"],
                ),
                content={"claim": row["text"], "period": row["period"]},
                content_text=row["text"], idempotency_key=f"report-memory:{run_id}:{row['id']}",
                confidence=row["confidence"], period=row["period"], source_run_id=run_id,
                claim_ids=[row["id"]], evidence_ids=[row["evidence_id"]],
            )))
        return results
