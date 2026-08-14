from backend.auth.models import PrincipalContext
from backend.jobs.dispatch import OutboxDispatcher


class LedgerStub:
    def __init__(self): self.marked = []
    def unpublished(self, principal, limit=100): return ["j1", "j2"]
    def mark_published(self, principal, job_id): self.marked.append(job_id); return True


def test_dispatcher_publishes_only_job_ids_and_marks_after_send():
    ledger, sent = LedgerStub(), []
    count = OutboxDispatcher(ledger, sent.append).publish_pending(
        PrincipalContext("u", "t", "owner")
    )
    assert count == 2 and sent == ["j1", "j2"] and ledger.marked == sent


def test_broker_failure_leaves_outbox_unmarked():
    ledger = LedgerStub()
    def fail(job_id): raise ConnectionError("redis down")
    try:
        OutboxDispatcher(ledger, fail).publish_pending(PrincipalContext("u", "t", "owner"))
    except ConnectionError:
        pass
    assert ledger.marked == []
