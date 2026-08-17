"""Single source of truth for the run state machine.

The transition rules previously lived in three places — the SQLite
``Repository`` (``database.py``), the PostgreSQL repository
(``db/durable.py``) and the SQLite migration trigger (``migrations.py``) —
and had already drifted (``database.py`` missed ``running -> completed``).
Everything now derives from this module: the Python CAS guards import these
sets, and the migration trigger is generated from them, so a rule change
lands in exactly one place.
"""

from __future__ import annotations

RUN_STATES: frozenset[str] = frozenset({
    "running", "pause_requested", "paused", "resuming", "failed", "completed",
})

TERMINAL_RUN_STATES: frozenset[str] = frozenset({"failed", "completed"})

# Ordinary state edges: every transition that completes or changes phase.
# ``running -> completed`` is deliberately present (it was missing in the
# SQLite copy of this table) — the completion path still requires the full
# side effects of complete_run_atomic before it is emitted.
RUN_STATE_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("running", "pause_requested"),
    ("pause_requested", "paused"),
    ("paused", "resuming"),
    ("resuming", "running"),
    ("running", "completed"),
    ("running", "failed"),
    ("pause_requested", "failed"),
    ("resuming", "failed"),
})

# Lease-recovery takeover edges: an expired lease lets a new owner claim the
# run by moving it straight to ``resuming``. These are guarded by
# ``recovery_required = 1`` in the migration trigger; the Python CAS path
# never emits them directly (takeover is a dedicated SQL update).
RECOVERY_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("running", "resuming"),
    ("pause_requested", "resuming"),
})

ALL_TRANSITIONS: frozenset[tuple[str, str]] = (
    RUN_STATE_TRANSITIONS | RECOVERY_TRANSITIONS
)
