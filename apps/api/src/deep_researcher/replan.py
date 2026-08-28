"""Controlled, append-only plan revision submission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import ResearchPlan, ResearchRun, ResearchTask

TriggerType = str
VALID_TRIGGER_TYPES = frozenset(
    {"blocking_failure", "evidence_gap", "evidence_conflict"}
)


@dataclass(frozen=True)
class ReplanLimits:
    max_replans: int = 4
    max_total_tasks: int = 16
    max_tasks_per_revision: int = 8
    max_changes_per_revision: int = 4


@dataclass(frozen=True)
class ReplanRequest:
    expected_plan_revision: int
    trigger_type: TriggerType
    trigger_ref: str
    goal: str
    tasks: tuple[Mapping[str, object], ...]
    allowed_tools: frozenset[str] = frozenset()
    remaining_tokens: int | None = None
    remaining_cost_micros: int | None = None
    deadline_available: bool = True
    lease_owner: str | None = None
    fencing_epoch: int | None = None


@dataclass(frozen=True)
class ReplanDecision:
    accepted: bool
    reason: str
    plan: ResearchPlan | None = None
    existing_version: int | None = None
    decision_ref: str | None = None


class ReplanRejected(ValueError):
    """Raised for malformed replans that cannot be submitted."""


class ReplanGate:
    """Validate a replan before the model-generated plan is committed."""

    def __init__(self, limits: ReplanLimits | None = None) -> None:
        self.limits = limits or ReplanLimits()

    def validate(
        self,
        session: Session,
        run: ResearchRun,
        request: ReplanRequest,
        current: ResearchPlan,
    ) -> str | None:
        if request.trigger_type not in VALID_TRIGGER_TYPES:
            return "invalid_trigger_type"
        if not request.trigger_ref.strip():
            return "missing_trigger_reference"
        if request.expected_plan_revision != current.version:
            return "plan_revision_conflict"
        if current.run_id != run.id:
            return "plan_run_mismatch"
        if (request.lease_owner is None) != (request.fencing_epoch is None):
            return "missing_worker_fence"
        if request.lease_owner is not None and (
            run.lease_owner != request.lease_owner
            or run.attempt != request.fencing_epoch
        ):
            return "stale_worker_fence"
        if run.reservation_status in {"exhausted", "released"}:
            return "budget_exhausted"
        if request.remaining_tokens is not None and request.remaining_tokens <= 0:
            return "token_budget_exhausted"
        if request.remaining_cost_micros is not None and request.remaining_cost_micros <= 0:
            return "cost_budget_exhausted"
        if not request.deadline_available:
            return "deadline_exhausted"
        if len(request.tasks) > self.limits.max_tasks_per_revision:
            return "revision_task_limit"
        if not request.tasks:
            return "empty_plan"

        current_count = len(
            session.scalars(select(ResearchTask).where(ResearchTask.run_id == run.id)).all()
        )
        if current_count + len(request.tasks) > self.limits.max_total_tasks:
            return "run_task_limit"

        snapshot_data = _canonical_snapshot(request.goal, request.tasks)
        if snapshot_data["plan_hash"] == current.plan_hash:
            return "equivalent_plan"

        previous_ordinals = {
            _int_value(item.get("ordinal"))
            for item in _snapshot_tasks(current.snapshot)
            if "ordinal" in item
        }
        new_ordinals = {
            _int_value(item.get("ordinal")) for item in request.tasks if "ordinal" in item
        }
        if len(new_ordinals) != len(request.tasks):
            return "invalid_task_ordinal"
        if len(new_ordinals & previous_ordinals) > self.limits.max_changes_per_revision:
            return "too_many_replacements"
        if len(new_ordinals - previous_ordinals) > self.limits.max_changes_per_revision:
            return "too_many_additions"
        for task in request.tasks:
            tools = _list_value(task, "allowed_tools")
            if not set(map(str, tools)).issubset(request.allowed_tools):
                return "tool_allowlist_violation"
        return None

    def submit(
        self,
        session_factory: sessionmaker[Session],
        run_id: UUID,
        request: ReplanRequest,
    ) -> ReplanDecision:
        try:
            with session_factory.begin() as session:
                run = session.scalar(
                    select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
                )
                if run is None:
                    raise ReplanRejected(f"research run not found: {run_id}")
                current = session.scalar(
                    select(ResearchPlan)
                    .where(
                        ResearchPlan.run_id == run_id,
                        ResearchPlan.version == request.expected_plan_revision,
                    )
                    .with_for_update()
                )
                latest = session.scalar(
                    select(ResearchPlan)
                    .where(ResearchPlan.run_id == run_id)
                    .order_by(ResearchPlan.version.desc())
                )
                if current is None:
                    return ReplanDecision(
                        False,
                        "plan_revision_conflict",
                        existing_version=latest.version if latest else None,
                    )
                if latest is None:
                    raise ReplanRejected("research plan not found")
                if latest.version != current.version:
                    return ReplanDecision(
                        False,
                        "plan_revision_conflict",
                        existing_version=latest.version,
                    )
                reason = self.validate(session, run, request, current)
                if reason is not None:
                    return ReplanDecision(
                        False,
                        reason,
                        existing_version=(
                            current.version if reason == "plan_revision_conflict" else None
                        ),
                    )
                if current.version - 1 >= self.limits.max_replans:
                    return ReplanDecision(False, "replan_limit")

                snapshot_data = _canonical_snapshot(request.goal, request.tasks)
                inherited = sorted(
                    _int_value(item.get("ordinal"))
                    for item in request.tasks
                    if bool(item.get("inherit", False))
                )
                replaced = sorted(
                    _int_value(item.get("ordinal"))
                    for item in request.tasks
                    if not bool(item.get("inherit", False))
                )
                plan = ResearchPlan(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    version=current.version + 1,
                    parent_version=current.version,
                    status="active",
                    trigger_type=request.trigger_type,
                    trigger_ref=request.trigger_ref,
                    inherited_task_ordinals=inherited,
                    replaced_task_ordinals=replaced,
                    goal=request.goal,
                    plan_hash=str(snapshot_data["plan_hash"]),
                    snapshot=snapshot_data["snapshot"],
                )
                session.add(plan)
                session.flush()
                for task in request.tasks:
                    session.add(
                        ResearchTask(
                            workspace_id=run.workspace_id,
                            run_id=run.id,
                            plan_id=plan.id,
                            plan_version=plan.version,
                            ordinal=_int_value(task.get("ordinal")),
                            title=str(task.get("title", "")),
                            goal=str(task.get("goal", "")),
                            success_criteria=[
                                str(value) for value in _list_value(task, "success_criteria")
                            ],
                            dependencies=[
                                _int_value(value) for value in _list_value(task, "dependencies")
                            ],
                            local_budget=_dict_value(task, "local_budget"),
                            role=str(task.get("role", "researcher")),
                            depth=_int_value(task.get("depth", 1)),
                            token_budget=_int_value(task.get("token_budget", 2000)),
                            time_budget_seconds=_int_value(task.get("time_budget_seconds", 30)),
                            allowed_tools=[
                                str(value) for value in _list_value(task, "allowed_tools")
                            ],
                            status="pending" if _list_value(task, "dependencies") else "ready",
                        )
                    )
                return ReplanDecision(True, "accepted", plan=plan)
        except IntegrityError:
            return ReplanDecision(False, "plan_revision_conflict")


PlanRevisionService = ReplanGate
Replanner = ReplanGate


def _canonical_snapshot(
    goal: str, tasks: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    normalized = [dict(task) for task in tasks]
    snapshot: dict[str, object] = {"version": 1, "goal": goal, "tasks": normalized}
    canonical = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return {"snapshot": snapshot, "plan_hash": hashlib.sha256(canonical.encode()).hexdigest()}


def _snapshot_tasks(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = snapshot.get("tasks", [])
    if not isinstance(raw, list):
        return []
    return [cast(Mapping[str, object], item) for item in raw if isinstance(item, dict)]


def _list_value(task: Mapping[str, object], key: str) -> list[object]:
    value = task.get(key, [])
    return cast(list[object], value) if isinstance(value, list) else []


def _dict_value(task: Mapping[str, object], key: str) -> dict[str, object]:
    value = task.get(key, {})
    return value if isinstance(value, dict) else {}


def _int_value(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) else default
