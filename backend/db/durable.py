from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import Engine, and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from backend.auth.models import PrincipalContext
from backend.db.metadata import (
    job_outbox, jobs,
    research_checkpoints_pg, research_events_pg, research_leases_pg,
    execution_authorizations_pg,
    research_plans_pg, research_runs_pg, research_steps_pg,
)
from backend.db.session import principal_transaction
from backend.run_states import RUN_STATE_TRANSITIONS


LEGAL_EDGES = RUN_STATE_TRANSITIONS


def _now() -> datetime: return datetime.now(timezone.utc)
def _hash(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
def _json(value) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _fingerprint(value) -> str: return _hash(_json(value))


@dataclass(frozen=True)
class DurableCreated:
    run_id: str
    lease_token: str
    created: bool
    job_id: str | None = None


class DurableConflict(RuntimeError): pass


class PostgresDurableRepository:
    """SQLAlchemy contract shared by SQLite unit tests and PostgreSQL/RLS runtime."""
    def __init__(self, engine: Engine, *, lease_seconds: int = 30):
        self.engine, self.lease_seconds = engine, lease_seconds

    def create_run(
        self, principal: PrincipalContext, *, company: str, question: str,
        idempotency_key: str, plan: dict, owner_id: str,
        enqueue_kind: str | None = None, max_attempts: int = 3,
    ) -> DurableCreated:
        request_fp = _fingerprint({"company": company, "question": question, "plan": plan})
        now, run_id, token = _now(), str(uuid4()), secrets.token_urlsafe(32)
        try:
            with principal_transaction(self.engine, principal) as connection:
                existing = connection.execute(select(
                    research_runs_pg.c.id, research_runs_pg.c.request_fingerprint,
                    research_leases_pg.c.token_hash,
                ).outerjoin(
                    research_leases_pg, research_leases_pg.c.run_id == research_runs_pg.c.id,
                ).where(and_(
                    research_runs_pg.c.tenant_id == principal.tenant_id,
                    research_runs_pg.c.created_by == principal.user_id,
                    research_runs_pg.c.idempotency_key == idempotency_key,
                ))).one_or_none()
                if existing is not None:
                    if existing.request_fingerprint != request_fp:
                        raise DurableConflict("idempotency key reused with different request")
                    existing_job = connection.execute(select(jobs.c.id).where(and_(
                        jobs.c.id == existing.id, jobs.c.tenant_id == principal.tenant_id,
                    ))).scalar_one_or_none()
                    if enqueue_kind is not None and existing_job is None:
                        raise DurableConflict("idempotent run is missing its durable job")
                    return DurableCreated(existing.id, "", False, existing_job)
                connection.execute(insert(research_runs_pg).values(
                    id=run_id, tenant_id=principal.tenant_id, created_by=principal.user_id,
                    idempotency_key=idempotency_key, request_fingerprint=request_fp,
                    status="running", company=company, question=question,
                    created_at=now, updated_at=now,
                ))
                connection.execute(insert(research_plans_pg).values(
                    run_id=run_id, tenant_id=principal.tenant_id, version=1,
                    plan_json=_json(plan), created_at=now,
                ))
                connection.execute(insert(research_checkpoints_pg).values(
                    run_id=run_id, tenant_id=principal.tenant_id, version=1,
                    next_pointer="start", state_json=_json({"completed_step_ids": []}), updated_at=now,
                ))
                connection.execute(insert(research_leases_pg).values(
                    run_id=run_id, tenant_id=principal.tenant_id, owner_id=owner_id,
                    token_hash=_hash(token), expires_at=now + timedelta(seconds=self.lease_seconds),
                ))
                self._event(connection, principal, run_id, "run.running", {"state_version": 0})
                if enqueue_kind is not None:
                    connection.execute(insert(jobs).values(
                        id=run_id, tenant_id=principal.tenant_id, created_by=principal.user_id,
                        kind=enqueue_kind, payload_json=_json({"run_id": run_id}), status="pending",
                        max_attempts=max_attempts, next_attempt_at=now, created_at=now, updated_at=now,
                    ))
                    connection.execute(insert(job_outbox).values(
                        id=str(uuid4()), tenant_id=principal.tenant_id, job_id=run_id, created_at=now,
                    ))
        except IntegrityError as exc:
            raise DurableConflict("concurrent run creation conflict") from exc
        return DurableCreated(run_id, token, True, run_id if enqueue_kind is not None else None)

    def acquire_run_lease_for_job(
        self, principal: PrincipalContext, run_id: str, *, job_id: str,
        job_claim_token: str,
        owner_id: str,
    ) -> str:
        """Exchange a live job claim for a fenced run lease; job_id alone is inert."""
        now, lease_token = _now(), secrets.token_urlsafe(32)
        with principal_transaction(self.engine, principal) as connection:
            job = connection.execute(select(jobs.c.id, jobs.c.payload_json).where(and_(
                jobs.c.id == job_id, jobs.c.tenant_id == principal.tenant_id,
                jobs.c.created_by == principal.user_id, jobs.c.status == "running",
                jobs.c.claim_token_hash == _hash(job_claim_token), jobs.c.claim_expires_at >= now,
            ))).one_or_none()
            run = connection.execute(select(
                research_runs_pg.c.status, research_runs_pg.c.state_version,
            ).where(and_(
                research_runs_pg.c.id == run_id,
                research_runs_pg.c.tenant_id == principal.tenant_id,
                research_runs_pg.c.created_by == principal.user_id,
            ))).one_or_none()
            if (
                job is None or json.loads(job.payload_json).get("run_id") != run_id
                or run is None or run.status not in {"running", "pause_requested", "resuming"}
            ):
                raise DurableConflict("job claim cannot acquire run lease")
            updated = connection.execute(update(research_leases_pg).where(and_(
                research_leases_pg.c.run_id == run_id,
                research_leases_pg.c.tenant_id == principal.tenant_id,
            )).values(
                owner_id=owner_id, token_hash=_hash(lease_token),
                expires_at=now + timedelta(seconds=self.lease_seconds),
            ))
            if updated.rowcount == 0:
                connection.execute(insert(research_leases_pg).values(
                    run_id=run_id, tenant_id=principal.tenant_id, owner_id=owner_id,
                    token_hash=_hash(lease_token),
                    expires_at=now + timedelta(seconds=self.lease_seconds),
                ))
            if run.status == "resuming":
                resumed = connection.execute(update(research_runs_pg).where(and_(
                    research_runs_pg.c.id == run_id,
                    research_runs_pg.c.tenant_id == principal.tenant_id,
                    research_runs_pg.c.status == "resuming",
                    research_runs_pg.c.state_version == run.state_version,
                )).values(
                    status="running", state_version=run.state_version + 1, updated_at=now,
                ))
                if resumed.rowcount != 1:
                    raise DurableConflict("resume state conflict")
                self._event(connection, principal, run_id, "run.running", {
                    "state_version": run.state_version + 1, "resumed_by_job": job_id,
                })
            self._event(connection, principal, run_id, "run.lease_acquired", {"owner_id": owner_id})
        return lease_token

    def resume_with_job(
        self, principal: PrincipalContext, run_id: str, *, expected_version: int,
        enqueue_kind: str, max_attempts: int = 3,
    ) -> str:
        now, job_id = _now(), str(uuid4())
        with principal_transaction(self.engine, principal) as connection:
            resumed = connection.execute(update(research_runs_pg).where(and_(
                research_runs_pg.c.id == run_id,
                research_runs_pg.c.tenant_id == principal.tenant_id,
                research_runs_pg.c.status == "paused",
                research_runs_pg.c.state_version == expected_version,
            )).values(
                status="resuming", state_version=expected_version + 1, updated_at=now,
            ))
            if resumed.rowcount != 1:
                raise DurableConflict("resume state conflict")
            connection.execute(insert(jobs).values(
                id=job_id, tenant_id=principal.tenant_id, created_by=principal.user_id,
                kind=enqueue_kind, payload_json=_json({"run_id": run_id}), status="pending",
                max_attempts=max_attempts, next_attempt_at=now, created_at=now, updated_at=now,
            ))
            connection.execute(insert(job_outbox).values(
                id=str(uuid4()), tenant_id=principal.tenant_id, job_id=job_id, created_at=now,
            ))
            self._event(connection, principal, run_id, "run.resuming", {
                "state_version": expected_version + 1, "job_id": job_id,
            })
        return job_id

    def get_run(self, principal: PrincipalContext, run_id: str) -> dict | None:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(research_runs_pg).where(and_(
                research_runs_pg.c.id == run_id,
                research_runs_pg.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
        return dict(row) if row else None

    def get_latest_plan(self, principal: PrincipalContext, run_id: str) -> dict | None:
        with principal_transaction(self.engine, principal) as connection:
            raw = connection.execute(select(research_plans_pg.c.plan_json).where(and_(
                research_plans_pg.c.run_id == run_id,
                research_plans_pg.c.tenant_id == principal.tenant_id,
            )).order_by(research_plans_pg.c.version.desc()).limit(1)).scalar_one_or_none()
        return json.loads(raw) if raw is not None else None

    def get_runtime_snapshot(self, principal: PrincipalContext, run_id: str) -> dict:
        """Return a durable-run snapshot compatible with the SQLite
        ``Repository.get_runtime_snapshot`` contract used by research
        processors (run / plan / checkpoint / lease / steps / counts).
        """
        with principal_transaction(self.engine, principal) as connection:
            run_row = connection.execute(select(research_runs_pg).where(and_(
                research_runs_pg.c.id == run_id,
                research_runs_pg.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
            if run_row is None:
                raise KeyError(run_id)
            plan_row = connection.execute(select(research_plans_pg.c.plan_json).where(and_(
                research_plans_pg.c.run_id == run_id,
                research_plans_pg.c.tenant_id == principal.tenant_id,
            )).order_by(research_plans_pg.c.version.desc()).limit(1)).mappings().one_or_none()
            checkpoint_row = connection.execute(select(research_checkpoints_pg).where(and_(
                research_checkpoints_pg.c.run_id == run_id,
                research_checkpoints_pg.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
            lease_row = connection.execute(select(research_leases_pg).where(and_(
                research_leases_pg.c.run_id == run_id,
                research_leases_pg.c.tenant_id == principal.tenant_id,
            ))).mappings().one_or_none()
            step_rows = connection.execute(select(research_steps_pg).where(and_(
                research_steps_pg.c.run_id == run_id,
                research_steps_pg.c.tenant_id == principal.tenant_id,
            )).order_by(research_steps_pg.c.created_at)).mappings().all()
        plan = dict(plan_row) if plan_row else None
        if plan:
            plan.update(json.loads(plan.pop("plan_json")))
        checkpoint = dict(checkpoint_row) if checkpoint_row else None
        if checkpoint:
            checkpoint["state"] = json.loads(checkpoint.pop("state_json"))
        steps = []
        for row in step_rows:
            step = dict(row)
            step["id"] = f"{run_id}:{step.get('step_id')}"
            step["output_json"] = step.pop("output_json")
            step["input_json"] = step.pop("input_json")
            steps.append(step)
        return {
            "run": dict(run_row),
            "plan": plan,
            "checkpoint": checkpoint,
            "lease": dict(lease_row) if lease_row else None,
            "steps": steps,
            "tool_calls": [],
            "counts": {"steps": len(steps), "tool_calls": 0},
        }

    def record_execution_authorization(
        self,
        *,
        run_id: str,
        plan_version: int,
        step_id: str,
        tool_name: str,
        allowed: bool,
        reason_codes: list[str],
        estimated_cost: int,
        budget_before: int,
        capability_token: str | None = None,
        effective_cost: int | None = None,
        budget_limit: int | None = None,
        principal=None,
    ) -> dict:
        """Persist a policy authorization decision for the controlled-tools
        pipeline. ``budget_limit`` is accepted for interface compatibility
        with the SQLite repository but the PG durable run stores the
        charged budget via ``commit_step``.
        """
        if principal is None:
            raise ValueError("record_execution_authorization requires principal")
        charged_cost = int(estimated_cost if effective_cost is None else effective_cost)
        now = _now()
        tenant_id = principal.tenant_id
        operation = {
            "run_id": run_id, "plan_version": plan_version, "step_id": step_id,
            "tool_name": tool_name, "decision": "allow" if allowed else "deny",
            "reason_codes": reason_codes, "estimated_cost": estimated_cost,
            "budget_before": budget_before, "effective_cost": charged_cost,
        }
        with principal_transaction(self.engine, principal) as connection:
            existing = connection.execute(select(execution_authorizations_pg).where(and_(
                execution_authorizations_pg.c.run_id == run_id,
                execution_authorizations_pg.c.tenant_id == tenant_id,
                execution_authorizations_pg.c.plan_version == plan_version,
                execution_authorizations_pg.c.step_id == step_id,
            ))).mappings().one_or_none()
            if existing is not None:
                decoded = dict(existing)
                decoded["reason_codes"] = json.loads(decoded.pop("reason_codes_json"))
                identity = {key: decoded[key] for key in operation}
                if identity != operation:
                    raise DurableConflict("authorization idempotency conflict")
                return decoded
            connection.execute(insert(execution_authorizations_pg).values(
                run_id=run_id, tenant_id=tenant_id, plan_version=plan_version,
                step_id=step_id, tool_name=tool_name,
                decision="allow" if allowed else "deny",
                reason_codes_json=_json(reason_codes),
                estimated_cost=estimated_cost, budget_before=budget_before,
                effective_cost=charged_cost, capability_token=capability_token,
                created_at=now,
            ))
            result = connection.execute(select(execution_authorizations_pg).where(and_(
                execution_authorizations_pg.c.run_id == run_id,
                execution_authorizations_pg.c.tenant_id == tenant_id,
                execution_authorizations_pg.c.plan_version == plan_version,
                execution_authorizations_pg.c.step_id == step_id,
            ))).mappings().one()
        decoded = dict(result)
        decoded["reason_codes"] = json.loads(decoded.pop("reason_codes_json"))
        return decoded

    def get_completed_step(
        self, principal: PrincipalContext, run_id: str, step_id: str,
    ) -> dict | None:
        with principal_transaction(self.engine, principal) as connection:
            row = connection.execute(select(
                research_steps_pg.c.input_json, research_steps_pg.c.output_json,
            ).where(and_(
                research_steps_pg.c.run_id == run_id,
                research_steps_pg.c.tenant_id == principal.tenant_id,
                research_steps_pg.c.step_id == step_id,
            ))).one_or_none()
        if row is None:
            return None
        return {"input": json.loads(row.input_json), "output": json.loads(row.output_json)}

    def renew_run_lease(
        self, principal: PrincipalContext, run_id: str, *, lease_token: str,
    ) -> bool:
        now = _now()
        with principal_transaction(self.engine, principal) as connection:
            result = connection.execute(update(research_leases_pg).where(and_(
                research_leases_pg.c.run_id == run_id,
                research_leases_pg.c.tenant_id == principal.tenant_id,
                research_leases_pg.c.token_hash == _hash(lease_token),
                research_leases_pg.c.expires_at >= now,
            )).values(expires_at=now + timedelta(seconds=self.lease_seconds)))
            return result.rowcount == 1

    def transition(
        self, principal: PrincipalContext, run_id: str, *, from_status: str,
        to_status: str, expected_version: int, lease_token: str | None = None,
    ) -> dict:
        if (from_status, to_status) not in LEGAL_EDGES:
            raise DurableConflict("illegal state transition")
        now = _now()
        with principal_transaction(self.engine, principal) as connection:
            if lease_token is not None and not self._valid_lease(connection, principal, run_id, lease_token, now):
                raise DurableConflict("lease lost")
            result = connection.execute(update(research_runs_pg).where(and_(
                research_runs_pg.c.id == run_id,
                research_runs_pg.c.tenant_id == principal.tenant_id,
                research_runs_pg.c.status == from_status,
                research_runs_pg.c.state_version == expected_version,
            )).values(status=to_status, state_version=expected_version + 1, updated_at=now))
            if result.rowcount != 1:
                raise DurableConflict("state version conflict")
            if to_status in {"paused", "failed", "completed"}:
                connection.execute(delete(research_leases_pg).where(and_(
                    research_leases_pg.c.run_id == run_id,
                    research_leases_pg.c.tenant_id == principal.tenant_id,
                )))
            self._event(connection, principal, run_id, f"run.{to_status}", {"state_version": expected_version + 1})
        return self.get_run(principal, run_id)

    def commit_step(
        self, principal: PrincipalContext, run_id: str, *, lease_token: str,
        step_id: str, step_input: dict, step_output: dict, next_pointer: str,
        progress: int, budget_delta: int, kind: str | None = None,
    ) -> dict:
        if not 0 <= progress <= 100 or budget_delta < 0:
            raise ValueError("invalid step progress or budget")
        now = _now()
        operation_fp = _fingerprint({
            "step_id": step_id, "input": step_input, "output": step_output,
            "next_pointer": next_pointer, "progress": progress, "budget_delta": budget_delta,
        })
        with principal_transaction(self.engine, principal) as connection:
            if not self._valid_lease(connection, principal, run_id, lease_token, now):
                raise DurableConflict("lease lost")
            existing = connection.execute(select(research_steps_pg.c.fingerprint).where(and_(
                research_steps_pg.c.run_id == run_id,
                research_steps_pg.c.tenant_id == principal.tenant_id,
                research_steps_pg.c.step_id == step_id,
            ))).scalar_one_or_none()
            if existing is not None:
                if existing != operation_fp:
                    raise DurableConflict("step idempotency conflict")
                run = connection.execute(select(
                    research_runs_pg.c.status, research_runs_pg.c.state_version,
                ).where(and_(
                    research_runs_pg.c.id == run_id,
                    research_runs_pg.c.tenant_id == principal.tenant_id,
                ))).one()
                if run.status == "pause_requested":
                    paused = connection.execute(update(research_runs_pg).where(and_(
                        research_runs_pg.c.id == run_id,
                        research_runs_pg.c.tenant_id == principal.tenant_id,
                        research_runs_pg.c.status == "pause_requested",
                        research_runs_pg.c.state_version == run.state_version,
                    )).values(
                        status="paused", state_version=run.state_version + 1, updated_at=now,
                    ))
                    if paused.rowcount != 1:
                        raise DurableConflict("pause state conflict")
                    connection.execute(delete(research_leases_pg).where(and_(
                        research_leases_pg.c.run_id == run_id,
                        research_leases_pg.c.tenant_id == principal.tenant_id,
                    )))
                    self._event(connection, principal, run_id, "run.paused", {
                        "state_version": run.state_version + 1, "after_step_id": step_id,
                    })
                return self._snapshot(connection, principal, run_id)
            run = connection.execute(select(
                research_runs_pg.c.status, research_runs_pg.c.state_version,
                research_runs_pg.c.budget_used,
            ).where(and_(
                research_runs_pg.c.id == run_id,
                research_runs_pg.c.tenant_id == principal.tenant_id,
            ))).one_or_none()
            if run is None or run.status not in {"running", "pause_requested"}:
                raise DurableConflict("run cannot accept a step")
            connection.execute(insert(research_steps_pg).values(
                run_id=run_id, tenant_id=principal.tenant_id, step_id=step_id,
                fingerprint=operation_fp, input_json=_json(step_input), output_json=_json(step_output),
                created_at=now,
            ))
            checkpoint = connection.execute(select(research_checkpoints_pg.c.version).where(and_(
                research_checkpoints_pg.c.run_id == run_id,
                research_checkpoints_pg.c.tenant_id == principal.tenant_id,
            ))).scalar_one()
            connection.execute(update(research_checkpoints_pg).where(and_(
                research_checkpoints_pg.c.run_id == run_id,
                research_checkpoints_pg.c.tenant_id == principal.tenant_id,
                research_checkpoints_pg.c.version == checkpoint,
            )).values(
                version=checkpoint + 1, next_pointer=next_pointer,
                state_json=_json({"last_step_id": step_id, "last_output": step_output}), updated_at=now,
            ))
            target = "paused" if run.status == "pause_requested" else run.status
            updated = connection.execute(update(research_runs_pg).where(and_(
                research_runs_pg.c.id == run_id,
                research_runs_pg.c.tenant_id == principal.tenant_id,
                research_runs_pg.c.state_version == run.state_version,
                research_runs_pg.c.status == run.status,
            )).values(
                status=target, state_version=run.state_version + (1 if target != run.status else 0),
                progress=progress, budget_used=run.budget_used + budget_delta, updated_at=now,
            ))
            if updated.rowcount != 1:
                raise DurableConflict("concurrent step commit")
            if target == "paused":
                connection.execute(delete(research_leases_pg).where(and_(
                    research_leases_pg.c.run_id == run_id,
                    research_leases_pg.c.tenant_id == principal.tenant_id,
                )))
            self._event(connection, principal, run_id, "step.completed", {"step_id": step_id})
            if target == "paused":
                self._event(connection, principal, run_id, "run.paused", {
                    "state_version": run.state_version + 1, "after_step_id": step_id,
                })
            return self._snapshot(connection, principal, run_id)

    @staticmethod
    def _valid_lease(connection, principal, run_id, token, now) -> bool:
        return connection.execute(select(research_leases_pg.c.run_id).where(and_(
            research_leases_pg.c.run_id == run_id,
            research_leases_pg.c.tenant_id == principal.tenant_id,
            research_leases_pg.c.token_hash == _hash(token),
            research_leases_pg.c.expires_at >= now,
        ))).one_or_none() is not None

    @staticmethod
    def _event(connection, principal, run_id, event_type, payload):
        connection.execute(insert(research_events_pg).values(
            id=str(uuid4()), run_id=run_id, tenant_id=principal.tenant_id,
            event_type=event_type, payload_json=_json(payload), created_at=_now(),
        ))

    @staticmethod
    def _snapshot(connection, principal, run_id):
        run = connection.execute(select(research_runs_pg).where(and_(
            research_runs_pg.c.id == run_id,
            research_runs_pg.c.tenant_id == principal.tenant_id,
        ))).mappings().one()
        checkpoint = connection.execute(select(research_checkpoints_pg).where(and_(
            research_checkpoints_pg.c.run_id == run_id,
            research_checkpoints_pg.c.tenant_id == principal.tenant_id,
        ))).mappings().one()
        return {"run": dict(run), "checkpoint": dict(checkpoint)}
