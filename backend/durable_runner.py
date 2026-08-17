from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.database import Repository
from backend.run_states import RUN_STATES, TERMINAL_RUN_STATES
from backend.schemas import ResearchCreate


SIX_RUN_STATES = RUN_STATES


class RunConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class CreatedRun:
    run: dict
    lease_token: str
    created: bool


@dataclass(frozen=True)
class RecoveredRun:
    run: dict
    lease_token: str


class DurableRunner:
    def __init__(self, repository: Repository, *, lease_ttl: timedelta | None = None):
        self.repository = repository
        self.lease_ttl = lease_ttl or timedelta(seconds=30)

    def _lease_expiry(self) -> str:
        return (datetime.now(timezone.utc) + self.lease_ttl).isoformat()

    def create_run(
        self,
        request: ResearchCreate,
        *,
        owner_id: str,
        idempotency_key: str,
        case_id: str | None = None,
        intake_id: str | None = None,
        initial_plan: dict | None = None,
    ) -> CreatedRun:
        if not owner_id.strip() or not idempotency_key.strip():
            raise ValueError("owner_id and idempotency_key are required")
        run, token, created = self.repository.create_run_atomic(
            request,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            lease_token=str(uuid4()),
            lease_expires_at=self._lease_expiry(),
            case_id=case_id,
            intake_id=intake_id,
            initial_plan=initial_plan,
        )
        return CreatedRun(run=run, lease_token=token, created=created)

    def request_pause(self, run_id: str) -> dict:
        run = self.repository.get_task(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] in {"pause_requested", "paused"}:
            return run
        if run["status"] != "running":
            raise RunConflict(f"cannot pause run in {run['status']}")
        try:
            return self.repository.cas_transition(
                run_id,
                from_statuses=("running",),
                to_status="pause_requested",
                kind="run.pause_requested",
                message="已请求暂停，等待安全检查点",
                expected_version=run["state_version"],
            )
        except ValueError as exc:
            latest = self.repository.get_task(run_id)
            if latest and latest["status"] in {"pause_requested", "paused"}:
                return latest
            raise RunConflict(str(exc)) from exc

    def acknowledge_pause(self, run_id: str, *, lease_token: str) -> dict:
        run = self.repository.get_task(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] == "paused":
            return run
        if run["status"] != "pause_requested":
            raise RunConflict(f"cannot acknowledge pause from {run['status']}")
        try:
            return self.repository.cas_transition(
                run_id,
                from_statuses=("pause_requested",),
                to_status="paused",
                kind="run.paused",
                message="研究已在安全检查点暂停",
                expected_version=run["state_version"],
                lease_token=lease_token,
                clear_recovery_required=True,
                delete_lease=True,
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def request_resume(self, run_id: str, *, owner_id: str) -> dict:
        run = self.repository.get_task(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] == "resuming":
            snapshot = self.repository.get_runtime_snapshot(run_id)
            lease = snapshot["lease"]
            if lease is None or lease["owner_id"] != owner_id:
                raise RunConflict("run is already resuming under another owner")
            return {**run, "lease_token": lease["lease_token"]}
        if run["status"] != "paused":
            raise RunConflict(f"cannot resume run in {run['status']}")
        token = str(uuid4())
        try:
            resumed = self.repository.cas_transition(
                run_id,
                from_statuses=("paused",),
                to_status="resuming",
                kind="run.resuming",
                message="正在校验检查点并恢复研究",
                expected_version=run["state_version"],
                new_lease=(owner_id, token, self._lease_expiry()),
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc
        return {**resumed, "lease_token": token}

    def finish_resume(self, run_id: str, *, lease_token: str) -> dict:
        run = self.repository.get_task(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] == "running":
            return run
        if run["status"] != "resuming":
            raise RunConflict(f"cannot finish resume from {run['status']}")
        try:
            self.validate_recovery_snapshot(run_id)
            return self.repository.cas_transition(
                run_id,
                from_statuses=("resuming",),
                to_status="running",
                kind="run.running",
                message="检查点校验完成，研究继续运行",
                expected_version=run["state_version"],
                lease_token=lease_token,
                clear_recovery_required=True,
            )
        except (ValueError, PermissionError, KeyError) as exc:
            latest = self.repository.get_task(run_id)
            if latest is not None and latest["status"] == "resuming":
                self.fail_run(
                    run_id,
                    lease_token=lease_token,
                    error=f"Recovery validation failed: {exc}",
                )
            raise RunConflict(str(exc)) from exc

    def validate_recovery_snapshot(self, run_id: str) -> dict:
        snapshot = self.repository.get_runtime_snapshot(run_id)
        checkpoint = snapshot["checkpoint"]
        if checkpoint is None:
            raise ValueError("missing checkpoint")
        if checkpoint["state_version"] > snapshot["run"]["state_version"]:
            raise ValueError("checkpoint is ahead of run state")
        frontier = checkpoint["frontier"]
        if not isinstance(frontier, dict):
            raise ValueError("checkpoint frontier is invalid")
        plan = snapshot["plan"]
        if plan is None or int(plan["version"]) != int(checkpoint["plan_version"]):
            raise ValueError("checkpoint plan version is not the latest plan")
        if int(frontier.get("plan_version") or 1) != int(plan["version"]):
            raise ValueError("frontier plan version is inconsistent")
        if snapshot["run"]["frontier"] != frontier:
            raise ValueError("run frontier differs from the latest checkpoint")
        state = checkpoint["state"]
        if state.get("frontier") != frontier:
            raise ValueError("checkpoint state frontier is inconsistent")
        if int(state.get("budget_used", 0)) != int(snapshot["run"]["budget_used"]):
            raise ValueError("checkpoint budget differs from the run budget")
        succeeded_steps = {
            row["id"].removeprefix(f"{run_id}:")
            for row in snapshot["steps"]
            if row["status"] == "succeeded"
        }
        completed_steps = set(frontier.get("completed_step_ids", []))
        if not completed_steps <= succeeded_steps and not state.get("migrated_from_legacy"):
            raise ValueError("frontier references uncommitted completed steps")
        if any(row["status"] != "succeeded" for row in snapshot["tool_calls"]):
            raise ValueError("tool call ledger contains an unfinished call")
        return snapshot

    def fail_run(self, run_id: str, *, lease_token: str, error: str) -> dict:
        run = self.repository.get_task(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] == "failed":
            return run
        if run["status"] not in {"running", "pause_requested", "resuming"}:
            raise RunConflict(f"cannot fail run in {run['status']}")
        try:
            return self.repository.cas_transition(
                run_id,
                from_statuses=(run["status"],),
                to_status="failed",
                kind="run.failed",
                message=f"研究执行失败：{error}",
                step="failed",
                expected_version=run["state_version"],
                lease_token=lease_token,
                delete_lease=True,
                error=error,
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def renew_lease(self, run_id: str, *, lease_token: str) -> dict:
        try:
            return self.repository.renew_lease(
                run_id,
                lease_token=lease_token,
                expires_at=self._lease_expiry(),
            )
        except PermissionError as exc:
            raise RunConflict(str(exc)) from exc

    def take_over_expired_run(self, run_id: str, *, owner_id: str, grace_seconds: float = 0.0) -> dict:
        token = str(uuid4())
        try:
            run, previous_status = self.repository.take_over_expired_lease(
                run_id,
                owner_id=owner_id,
                lease_token=token,
                expires_at=self._lease_expiry(),
                grace_seconds=grace_seconds,
            )
        except (PermissionError, ValueError) as exc:
            raise RunConflict(str(exc)) from exc
        return {"run": run, "lease_token": token, "previous_status": previous_status}

    def commit_step(
        self,
        run_id: str,
        *,
        lease_token: str,
        step_id: str,
        kind: str,
        step_input: dict,
        step_output: dict,
        idempotency_key: str,
        frontier: dict,
        progress: int,
        budget_delta: int = 0,
        tool: dict | None = None,
        capability_token: str | None = None,
        tool_commit_token: str | None = None,
    ) -> dict:
        try:
            return self.repository.commit_step_atomic(
                run_id,
                lease_token=lease_token,
                step_id=step_id,
                kind=kind,
                step_input=step_input,
                step_output=step_output,
                idempotency_key=idempotency_key,
                frontier=frontier,
                progress=progress,
                budget_delta=budget_delta,
                tool=tool,
                capability_token=capability_token,
                tool_commit_token=tool_commit_token,
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def install_plan(self, run_id: str, *, lease_token: str, plan: dict) -> dict:
        try:
            return self.repository.install_plan_atomic(
                run_id, lease_token=lease_token, plan=plan
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def complete_run(
        self,
        run_id: str,
        *,
        lease_token: str,
        result: dict,
        evidence: list[dict],
    ) -> dict:
        try:
            return self.repository.complete_run_atomic(
                run_id,
                lease_token=lease_token,
                result=result,
                evidence=evidence,
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def persist_verified_evidence(self, run_id: str, *, lease_token: str, evidence, claims) -> None:
        try:
            self.repository.persist_verified_evidence(
                run_id, lease_token=lease_token, evidence=evidence, claims=claims
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def persist_report_snapshot(
        self, run_id: str, *, lease_token: str, generation_key: str,
        model: str, schema_version: int, snapshot: dict,
    ) -> dict:
        try:
            return self.repository.persist_report_snapshot_atomic(
                run_id, lease_token=lease_token, generation_key=generation_key,
                model=model, schema_version=schema_version, snapshot=snapshot,
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def complete_verified_report(
        self, run_id: str, *, lease_token: str, generation_key: str,
        markdown: str, report_json: dict, citations: list[dict], degraded: bool,
    ) -> dict:
        try:
            return self.repository.complete_verified_report_atomic(
                run_id, lease_token=lease_token, generation_key=generation_key,
                markdown=markdown, report_json=report_json, citations=citations,
                degraded=degraded,
            )
        except (ValueError, PermissionError) as exc:
            raise RunConflict(str(exc)) from exc

    def reconcile_expired_runs(self, *, owner_id: str) -> list[RecoveredRun]:
        # An expired lease is only claimable after a grace window (one third of
        # the lease TTL), so a worker whose heartbeat merely lagged is not
        # immediately stripped of its run by the reconciler.
        grace = max(0.0, self.lease_ttl.total_seconds() / 3)
        recovered: list[RecoveredRun] = []
        for run_id in self.repository.list_recovery_candidates():
            try:
                takeover = self.take_over_expired_run(
                    run_id, owner_id=owner_id, grace_seconds=grace,
                )
            except RunConflict:
                continue
            token = takeover["lease_token"]
            previous_status = takeover["previous_status"]
            try:
                run = self.finish_resume(run_id, lease_token=token)
                if previous_status == "pause_requested":
                    self.request_pause(run_id)
                    self.acknowledge_pause(run_id, lease_token=token)
                else:
                    recovered.append(RecoveredRun(run=run, lease_token=token))
            except Exception as exc:
                # Recovery validation failed: mark the run failed. If even the
                # failure marker cannot be written (e.g. lease state raced),
                # swallow the error so one bad run never aborts the whole
                # startup reconciliation loop.
                try:
                    self.fail_run(
                        run_id, lease_token=token, error=f"Recovery failed: {exc}",
                    )
                except Exception:
                    pass
        return recovered
