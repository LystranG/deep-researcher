"""Durable Plan v1 and single-task runtime primitives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.agents.planner import TaskSpec, plan
from deep_researcher.models import (
    ResearchPlan,
    ResearchRun,
    ResearchTask,
    TaskOutcome,
)
from deep_researcher.models import (
    TaskClaim as TaskClaimRecord,
)

TERMINAL_TASK_KINDS = frozenset({"completed", "failed", "cancelled", "skipped", "superseded"})


class TaskSnapshot(TypedDict):
    ordinal: int
    role: str
    title: str
    goal: str
    success_criteria: list[str]
    dependencies: list[int]
    allowed_tools: list[str]
    local_budget: dict[str, int]
    depth: int


class StaleTaskClaimError(RuntimeError):
    """Raised when a worker tries to commit with an obsolete fencing epoch."""


@dataclass(frozen=True)
class TaskClaim:
    task_id: UUID
    run_id: UUID
    lease_owner: str
    fencing_epoch: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class TaskExecutionResult:
    kind: str = "completed"
    result_reference: str | None = None
    evidence_refs: tuple[str, ...] = ()
    failure_ref: str | None = None


class TaskAdapter(Protocol):
    def execute(self, claim: TaskClaim) -> TaskExecutionResult:
        """Perform one bounded task quantum and return a deterministic proposal."""


class DeterministicTaskAdapter:
    """Small adapter used by behavioral tests and local runtime smoke checks."""

    def __init__(self, result: TaskExecutionResult | None = None) -> None:
        self.result = result or TaskExecutionResult(result_reference="deterministic:completed")

    def execute(self, claim: TaskClaim) -> TaskExecutionResult:
        del claim
        return self.result


def _task_snapshot(spec: TaskSpec) -> TaskSnapshot:
    return {
        "ordinal": spec["ordinal"],
        "role": spec["role"],
        "title": spec["title"],
        "goal": spec["goal"],
        "success_criteria": list(spec["success_criteria"]),
        "dependencies": list(spec["dependencies"]),
        "allowed_tools": list(spec["allowed_tools"]),
        "local_budget": dict(spec["local_budget"]),
        "depth": spec["depth"],
    }


def persist_plan_v1(session: Session, run: ResearchRun, question: str) -> ResearchPlan:
    """Persist the deterministic initial plan exactly once in the run transaction."""
    existing = session.scalar(
        select(ResearchPlan).where(
            ResearchPlan.run_id == run.id,
            ResearchPlan.version == 1,
        )
    )
    if existing is not None:
        return existing

    specs = plan(question)["tasks"]
    tasks = [_task_snapshot(spec) for spec in specs]
    snapshot: dict[str, object] = {"version": 1, "goal": question, "tasks": tasks}
    canonical = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    research_plan = ResearchPlan(
        workspace_id=run.workspace_id,
        run_id=run.id,
        version=1,
        goal=question,
        plan_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        snapshot=snapshot,
    )
    session.add(research_plan)
    session.flush()
    for task in tasks:
        session.add(
            ResearchTask(
                workspace_id=run.workspace_id,
                run_id=run.id,
                plan_id=research_plan.id,
                plan_version=1,
                ordinal=int(task["ordinal"]),
                title=str(task["title"]),
                goal=str(task["goal"]),
                success_criteria=task["success_criteria"],
                dependencies=task["dependencies"],
                local_budget=task["local_budget"],
                role=str(task["role"]),
                depth=int(task["depth"]),
                token_budget=task["local_budget"]["token_budget"],
                time_budget_seconds=task["local_budget"]["time_budget_seconds"],
                allowed_tools=task["allowed_tools"],
                status="ready" if not task["dependencies"] else "pending",
            )
        )
    return research_plan


class TaskRuntime:
    """Claim, advance, and finalize tasks using only database facts."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def refresh_ready(self, run_id: UUID) -> list[UUID]:
        with self._session_factory.begin() as session:
            tasks = session.scalars(
                select(ResearchTask)
                .where(ResearchTask.run_id == run_id)
                .with_for_update()
            ).all()
            outcomes = {
                outcome.task_id: outcome.kind
                for outcome in session.scalars(
                    select(TaskOutcome).where(TaskOutcome.run_id == run_id)
                )
            }
            ready: list[UUID] = []
            for task in tasks:
                if task.status in TERMINAL_TASK_KINDS:
                    continue
                dependencies = _dependency_ids(tasks, task)
                if all(outcomes.get(task_id) == "completed" for task_id in dependencies):
                    if task.status == "pending":
                        task.status = "ready"
                    ready.append(task.id)
            return ready

    def claim(
        self,
        task_id: UUID,
        *,
        lease_owner: str,
        lease_seconds: int = 30,
    ) -> TaskClaim | None:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(ResearchTask).where(ResearchTask.id == task_id).with_for_update()
            )
            if task is None or task.status in TERMINAL_TASK_KINDS:
                return None
            if not _dependencies_completed(session, task):
                return None
            if (
                task.status == "running"
                and task.lease_expires_at is not None
                and _as_utc(task.lease_expires_at) > now
            ):
                if task.lease_owner != lease_owner:
                    return None
                return TaskClaim(
                    task_id=task.id,
                    run_id=task.run_id,
                    lease_owner=lease_owner,
                    fencing_epoch=task.fencing_epoch,
                    lease_expires_at=_as_utc(task.lease_expires_at),
                )
            task.fencing_epoch += 1
            task.status = "running"
            task.lease_owner = lease_owner
            task.lease_expires_at = now + timedelta(seconds=lease_seconds)
            claim = TaskClaimRecord(
                workspace_id=task.workspace_id,
                run_id=task.run_id,
                task_id=task.id,
                lease_owner=lease_owner,
                fencing_epoch=task.fencing_epoch,
                lease_expires_at=task.lease_expires_at,
            )
            session.add(claim)
            return TaskClaim(
                task_id=task.id,
                run_id=task.run_id,
                lease_owner=lease_owner,
                fencing_epoch=task.fencing_epoch,
                lease_expires_at=task.lease_expires_at,
            )

    def record_outcome(self, claim: TaskClaim, result: TaskExecutionResult) -> TaskOutcome:
        if result.kind not in TERMINAL_TASK_KINDS:
            raise ValueError(f"invalid task outcome kind: {result.kind}")
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(ResearchTask).where(ResearchTask.id == claim.task_id).with_for_update()
            )
            existing = session.scalar(
                select(TaskOutcome).where(TaskOutcome.task_id == claim.task_id)
            )
            if existing is not None:
                return existing
            run = session.get(ResearchRun, claim.run_id)
            if task is None or (
                task.lease_owner != claim.lease_owner
                or task.fencing_epoch != claim.fencing_epoch
                or task.lease_expires_at is None
                or _as_utc(task.lease_expires_at) < datetime.now(UTC)
                or run is None
                or run.cancel_requested_at is not None
                or run.status in {"cancelled", "completed", "partial", "failed"}
            ):
                raise StaleTaskClaimError("task claim has been fenced")
            outcome = TaskOutcome(
                workspace_id=task.workspace_id,
                run_id=task.run_id,
                task_id=task.id,
                fencing_epoch=claim.fencing_epoch,
                kind=result.kind,
                outcome_ref=f"task-outcome:{task.id}",
                result_reference=result.result_reference,
                evidence_refs=list(result.evidence_refs),
                failure_ref=result.failure_ref,
            )
            session.add(outcome)
            task.status = result.kind
            task.completed_at = datetime.now(UTC)
            task.lease_owner = None
            task.lease_expires_at = None
            persisted_claim = session.scalar(
                select(TaskClaimRecord).where(
                    TaskClaimRecord.task_id == task.id,
                    TaskClaimRecord.fencing_epoch == claim.fencing_epoch,
                )
            )
            if persisted_claim is not None:
                persisted_claim.released_at = datetime.now(UTC)
            session.flush()
            return outcome

    def advance(
        self,
        task_id: UUID,
        *,
        lease_owner: str,
        adapter: TaskAdapter,
        lease_seconds: int = 30,
    ) -> TaskOutcome | None:
        claim = self.claim(
            task_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return None
        return self.record_outcome(claim, adapter.execute(claim))


def _dependency_ids(tasks: Sequence[ResearchTask], task: ResearchTask) -> list[UUID]:
    by_ordinal = {candidate.ordinal: candidate.id for candidate in tasks}
    return [by_ordinal[ordinal] for ordinal in task.dependencies if ordinal in by_ordinal]


def _dependencies_completed(session: Session, task: ResearchTask) -> bool:
    if not task.dependencies:
        return True
    tasks = session.scalars(
        select(ResearchTask).where(ResearchTask.run_id == task.run_id)
    ).all()
    dependencies = _dependency_ids(tasks, task)
    if len(dependencies) != len(task.dependencies):
        return False
    completed = {
        outcome.task_id
        for outcome in session.scalars(
            select(TaskOutcome).where(
                TaskOutcome.run_id == task.run_id,
                TaskOutcome.kind == "completed",
            )
        )
    }
    return all(dependency in completed for dependency in dependencies)


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive datetime values to the runtime's UTC contract."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
