"""Durable Plan v1 and single-task runtime primitives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
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
    TaskModelTurn,
    TaskObservation,
    TaskOutcome,
    TaskResultProposalRecord,
)
from deep_researcher.models import (
    TaskClaim as TaskClaimRecord,
)

TERMINAL_TASK_KINDS = frozenset(
    {"completed", "partial", "failed", "cancelled", "skipped", "superseded"}
)
MAX_MODEL_TURNS = 8
NO_EVIDENCE_GAIN_LIMIT = 2


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
    run_fencing_epoch: int | None = None


@dataclass(frozen=True)
class TaskExecutionResult:
    kind: str = "completed"
    result_reference: str | None = None
    evidence_refs: tuple[str, ...] = ()
    failure_ref: str | None = None


@dataclass(frozen=True)
class TaskToolCall:
    """模型在一个 Model Turn 中提出的唯一规范化 Tool Call"""

    tool_name: str
    arguments: dict[str, object]
    provider_reference: str | None = None
    usage: dict[str, int | float] | None = None
    logical_call_ref: str | None = None
    parameters_hash: str | None = None
    safe_summary: str | None = None


@dataclass(frozen=True)
class TaskResultProposal:
    """模型提交给 Controller 的候选结果，不包含 Task Outcome 状态"""

    result_reference: str | None = None
    evidence_refs: tuple[str, ...] = ()
    covered_criteria: tuple[str, ...] = ()
    provider_reference: str | None = None
    usage: dict[str, int | float] | None = None


TaskModelOutput = TaskToolCall | TaskResultProposal


@dataclass(frozen=True)
class TaskObservationResult:
    """Tool Adapter 返回的、可持久化的 Observation 草稿"""

    status: str = "succeeded"
    result_reference: str | None = None
    evidence_refs: tuple[str, ...] = ()
    file_refs: tuple[dict[str, str], ...] = ()
    evidence_gain: bool = False
    failure_ref: str | None = None
    summary: str | None = None
    error_category: str | None = None
    waiting_reference: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    """Static contract and adapter binding exposed to a Research Task."""

    name: str
    input_schema: dict[str, object]
    result_contract: dict[str, object] | str = "observation"
    risk: str = "safe"
    handler: TaskToolAdapter | None = None
    requires_approval: bool = False
    description: str = ""
    output_schema: dict[str, object] | None = None
    annotations: dict[str, object] | None = None
    provider: str = "builtin"


@dataclass(frozen=True)
class ToolRegistrySnapshot:
    """Immutable tool definitions bound to one Model Turn."""

    snapshot_id: str
    definitions: tuple[ToolDefinition, ...]

    def get(self, name: str) -> ToolDefinition | None:
        return next(
            (definition for definition in self.definitions if definition.name == name),
            None,
        )

    def validate(self, definition: ToolDefinition, arguments: dict[str, object]) -> str | None:
        return _validate_tool_schema(definition.input_schema, arguments, "$")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.snapshot_id,
            "tools": [
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": deepcopy(definition.input_schema),
                    "output_schema": deepcopy(definition.output_schema),
                    "result_contract": deepcopy(definition.result_contract),
                    "risk": definition.risk,
                    "requires_approval": definition.requires_approval,
                    "annotations": deepcopy(definition.annotations),
                    "provider": definition.provider,
                }
                for definition in self.definitions
            ],
        }


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str | None = None
    waiting_reference: str | None = None


class ToolRegistry:
    """Small immutable-at-read static registry for task tools."""

    def __init__(self, definitions: Sequence[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or definition.name in self._definitions:
            raise ValueError(f"duplicate or empty tool definition: {definition.name!r}")
        if definition.risk not in {"safe", "review", "dangerous"}:
            raise ValueError(f"invalid tool risk: {definition.risk}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    def snapshot(self) -> ToolRegistrySnapshot:
        """Freeze definitions and copy schemas before a Model Turn starts."""
        payload = [
            {
                "name": definition.name,
                "input_schema": definition.input_schema,
                "output_schema": definition.output_schema,
                "result_contract": definition.result_contract,
                "risk": definition.risk,
                "requires_approval": definition.requires_approval,
                "annotations": definition.annotations,
                "provider": definition.provider,
            }
            for definition in self.definitions()
        ]
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        frozen = tuple(
            replace(
                definition,
                input_schema=deepcopy(definition.input_schema),
                output_schema=deepcopy(definition.output_schema),
                annotations=deepcopy(definition.annotations),
            )
            for definition in self.definitions()
        )
        return ToolRegistrySnapshot(hashlib.sha256(canonical.encode()).hexdigest(), frozen)

    def validate(self, definition: ToolDefinition, arguments: dict[str, object]) -> str | None:
        return _validate_tool_schema(definition.input_schema, arguments, "$")


class ToolPolicy:
    """Policy seam for task allowlists, workspace ACL, risk and tool budgets."""

    def __init__(
        self,
        *,
        workspace_acl: Callable[[UUID, str], bool] | None = None,
        risk_policy: Callable[[str], bool] | None = None,
        max_tool_calls: int | None = None,
        approval_reference: Callable[[TaskClaim, ToolDefinition], str] | None = None,
    ) -> None:
        if max_tool_calls is not None and max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        self._workspace_acl = workspace_acl
        self._risk_policy = risk_policy
        self._max_tool_calls = max_tool_calls
        self._approval_reference = approval_reference

    def check(
        self,
        claim: TaskClaim,
        task: ResearchTask,
        run: ResearchRun,
        definition: ToolDefinition,
        *,
        tool_call_count: int,
    ) -> ToolPolicyDecision:
        if run.cancel_requested_at is not None:
            return ToolPolicyDecision(False, "run_cancelled")
        if task.allowed_tools and definition.name not in task.allowed_tools:
            return ToolPolicyDecision(False, "tool_not_allowed")
        if self._workspace_acl is not None and not self._workspace_acl(
            task.workspace_id, definition.name
        ):
            return ToolPolicyDecision(False, "workspace_acl_denied")
        if self._max_tool_calls is not None and tool_call_count >= self._max_tool_calls:
            return ToolPolicyDecision(False, "tool_budget_exhausted")
        if self._risk_policy is not None and not self._risk_policy(definition.risk):
            return ToolPolicyDecision(False, "risk_policy_denied")
        if definition.requires_approval or definition.risk == "review":
            reference = (
                self._approval_reference(claim, definition)
                if self._approval_reference is not None
                else f"approval:{claim.task_id}:{definition.name}"
            )
            return ToolPolicyDecision(
                allowed=False,
                reason="approval_required",
                waiting_reference=reference,
            )
        return ToolPolicyDecision(True)


@dataclass(frozen=True)
class TaskTurnContext:
    """一次 Model Turn 允许看到的任务合同和已提交 Observation"""

    task_id: UUID
    run_id: UUID
    turn_ordinal: int
    goal: str
    success_criteria: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    previous_observation: TaskObservationResult | None = None
    tool_snapshot: ToolRegistrySnapshot | None = None


@dataclass(frozen=True)
class TaskAdvanceResult:
    """Controller 的有限推进结果"""

    status: str
    task_id: UUID
    turn_ordinal: int | None = None
    observation_ref: str | None = None
    outcome_ref: str | None = None
    reason: str | None = None


class InvalidTaskModelOutputError(ValueError):
    """模型没有返回规范化 Tool Call 或 Task Result Proposal"""


class TaskModelGateway(Protocol):
    """单 Task 的结构化模型 Adapter，不复用 Writer 流式回答协议"""

    def complete_task_turn(self, context: TaskTurnContext) -> object:
        """返回一个 Tool Call 或 Task Result Proposal"""

    def repair_task_turn(self, context: TaskTurnContext, invalid_output: object) -> object:
        """对当前 turn 的非法输出执行至多一次受控修复"""


class TaskToolAdapter(Protocol):
    """执行一个已通过 Task Controller 边界的同步 Tool Call"""

    def execute(self, claim: TaskClaim, call: TaskToolCall) -> TaskObservationResult:
        """返回结构化 Observation，不返回模型可直接使用的 Task Outcome"""

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


class DeterministicTaskModelGateway:
    """行为测试使用的确定性 Model Gateway"""

    def __init__(
        self,
        outputs: Sequence[object],
        *,
        repairs: Sequence[object] = (),
    ) -> None:
        self._outputs = list(outputs)
        self._repairs = list(repairs)
        self.contexts: list[TaskTurnContext] = []

    def complete_task_turn(self, context: TaskTurnContext) -> object:
        self.contexts.append(context)
        if not self._outputs:
            raise TimeoutError("deterministic model output exhausted")
        return self._outputs.pop(0)

    def repair_task_turn(self, context: TaskTurnContext, invalid_output: object) -> object:
        del context, invalid_output
        if not self._repairs:
            raise InvalidTaskModelOutputError("controlled repair output exhausted")
        return self._repairs.pop(0)


class DeterministicTaskToolAdapter:
    """行为测试使用的确定性 Tool Adapter"""

    def __init__(self, result: TaskObservationResult | None = None) -> None:
        self.result = result or TaskObservationResult(
            result_reference="deterministic:observation",
            evidence_refs=("evidence:deterministic",),
            evidence_gain=True,
        )
        self.calls: list[TaskToolCall] = []

    def execute(self, claim: TaskClaim, call: TaskToolCall) -> TaskObservationResult:
        del claim
        self.calls.append(call)
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
    """Schedule, claim, advance, and finalize tasks using database facts."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_fan_out: int = 4,
    ) -> None:
        if max_fan_out < 1:
            raise ValueError("max_fan_out must be greater than zero")
        self._session_factory = session_factory
        self._max_fan_out = max_fan_out

    def refresh_ready(self, run_id: UUID) -> list[UUID]:
        """Recover expired claims and return the current dependency-ready frontier.

        A task remains pending when a dependency has anything other than a
        completed outcome. This is deliberately stricter than looking at task
        status so failed, cancelled, incomplete, and superseded outcomes never
        unlock descendants.
        """
        now = datetime.now(UTC)
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
                if (
                    task.status == "running"
                    and task.lease_expires_at is not None
                    and _as_utc(task.lease_expires_at) <= now
                    and task.id not in outcomes
                ):
                    task.status = "ready"
                    task.lease_owner = None
                    task.lease_expires_at = None
                dependencies = _dependency_ids(tasks, task)
                if all(outcomes.get(task_id) == "completed" for task_id in dependencies):
                    if task.status in {"pending", "ready"}:
                        task.status = "ready"
                    if task.status == "ready":
                        ready.append(task.id)
            return ready

    def ready_frontier(self, run_id: UUID) -> list[UUID]:
        """Return all dependency-ready tasks; claims enforce the fan-out limit."""
        return self.refresh_ready(run_id)

    def barrier_satisfied(self, run_id: UUID) -> bool:
        """Whether every task in the current runnable stage has an outcome."""
        with self._session_factory() as session:
            tasks = session.scalars(
                select(ResearchTask)
                .where(ResearchTask.run_id == run_id)
                .order_by(ResearchTask.ordinal)
            ).all()
            outcomes = {
                outcome.task_id: outcome.kind
                for outcome in session.scalars(
                    select(TaskOutcome).where(TaskOutcome.run_id == run_id)
                )
            }
            active = [
                task
                for task in tasks
                if task.status in {"ready", "running"}
                or (
                    task.status == "pending"
                    and all(
                        outcomes.get(dependency_id) == "completed"
                        for dependency_id in _dependency_ids(tasks, task)
                    )
                )
            ]
            terminal_without_outcome = [
                task
                for task in tasks
                if task.status in TERMINAL_TASK_KINDS and task.id not in outcomes
            ]
            return not terminal_without_outcome and all(
                task.id in outcomes for task in active
            )

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
            run = session.get(ResearchRun, task.run_id) if task is not None else None
            if (
                task is None
                or run is None
                or task.status in TERMINAL_TASK_KINDS
                or run.status in {"cancelled", "completed", "partial", "failed"}
            ):
                return None
            if not _dependencies_completed(session, task):
                return None
            if task.status != "running":
                active_count = len(
                    session.scalars(
                        select(ResearchTask.id).where(
                            ResearchTask.run_id == task.run_id,
                            ResearchTask.status == "running",
                        )
                    ).all()
                )
                if active_count >= self._max_fan_out:
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
                    run_fencing_epoch=(
                        run.attempt if run.lease_owner == lease_owner else None
                    ),
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
                run_fencing_epoch=run.attempt if run.lease_owner == lease_owner else None,
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
                or (
                    claim.run_fencing_epoch is not None
                    and (
                        run.lease_owner != claim.lease_owner
                        or run.attempt != claim.run_fencing_epoch
                    )
                )
                or (
                    run.cancel_requested_at is not None
                    and result.kind != "cancelled"
                )
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


class ReActTaskController:
    """单个 ResearchTask 的可重入、有界 Model Turn 推进入口"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        model_gateway: TaskModelGateway,
        tool_adapter: TaskToolAdapter | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_policy: ToolPolicy | None = None,
        max_turns: int = MAX_MODEL_TURNS,
        no_evidence_gain_limit: int = NO_EVIDENCE_GAIN_LIMIT,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        if no_evidence_gain_limit < 1:
            raise ValueError("no_evidence_gain_limit must be positive")
        self._session_factory = session_factory
        self._model_gateway = model_gateway
        self._tool_adapter = tool_adapter
        self._tool_registry = tool_registry.snapshot() if tool_registry is not None else None
        self._tool_policy = tool_policy or ToolPolicy()
        self._max_turns = max_turns
        self._no_evidence_gain_limit = no_evidence_gain_limit
        self._task_runtime = TaskRuntime(session_factory)

    def advance(self, claim: TaskClaim) -> TaskAdvanceResult:
        """最多推进一个 Model Turn，并返回 runnable、waiting 或 terminal"""
        with self._session_factory() as session:
            task, run, existing = self._active_claim_state(session, claim)
            if existing is not None:
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=claim.task_id,
                    outcome_ref=existing.outcome_ref,
                )
            if run.cancel_requested_at is not None:
                outcome = self._task_runtime.record_outcome(
                    claim,
                    TaskExecutionResult(kind="cancelled", failure_ref="run_cancelled"),
                )
                return TaskAdvanceResult(
                    status="terminal", task_id=claim.task_id, outcome_ref=outcome.outcome_ref
                )
            turns = session.scalars(
                select(TaskModelTurn)
                .where(TaskModelTurn.task_id == task.id)
                .order_by(TaskModelTurn.turn_ordinal)
            ).all()
            latest_observation = session.scalar(
                select(TaskObservation)
                .where(TaskObservation.task_id == task.id)
                .order_by(TaskObservation.created_at.desc(), TaskObservation.id.desc())
            )
            latest_turn = session.scalar(
                select(TaskModelTurn)
                .where(TaskModelTurn.task_id == task.id)
                .order_by(TaskModelTurn.turn_ordinal.desc())
            )
            pending_proposal = session.scalar(
                select(TaskResultProposalRecord)
                .where(TaskResultProposalRecord.task_id == task.id)
                .order_by(
                    TaskResultProposalRecord.created_at.desc(),
                    TaskResultProposalRecord.id.desc(),
                )
            )
            if pending_proposal is not None:
                replay_outcome = self._task_runtime.record_outcome(
                    claim,
                    TaskExecutionResult(
                        kind="completed" if pending_proposal.valid else "partial",
                        result_reference=pending_proposal.result_reference,
                        evidence_refs=tuple(pending_proposal.evidence_refs),
                        failure_ref=pending_proposal.rejection_reason,
                    ),
                )
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=task.id,
                    outcome_ref=replay_outcome.outcome_ref,
                    reason=pending_proposal.rejection_reason,
                )
            no_gain_streak = _no_evidence_gain_streak(
                session, task.id, self._no_evidence_gain_limit
            )
            if len(turns) >= self._max_turns:
                reason = "turn_budget_exhausted"
                outcome = self._task_runtime.record_outcome(
                    claim, TaskExecutionResult(kind="failed", failure_ref=reason)
                )
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=task.id,
                    outcome_ref=outcome.outcome_ref,
                    reason=reason,
                )
            if no_gain_streak >= self._no_evidence_gain_limit:
                reason = "no_evidence_gain"
                outcome = self._task_runtime.record_outcome(
                    claim, TaskExecutionResult(kind="failed", failure_ref=reason)
                )
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=task.id,
                    outcome_ref=outcome.outcome_ref,
                    reason=reason,
                )
            budget_reason = _task_budget_reason(task, turns)
            if budget_reason is not None:
                outcome = self._task_runtime.record_outcome(
                    claim, TaskExecutionResult(kind="failed", failure_ref=budget_reason)
                )
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=task.id,
                    outcome_ref=outcome.outcome_ref,
                    reason=budget_reason,
                )
            if latest_observation is not None and latest_observation.status == "waiting":
                if latest_turn is not None:
                    resumed = self._resume_waiting_tool(
                        claim, latest_turn, latest_observation
                    )
                    if resumed is not None:
                        if resumed.status == "waiting":
                            return TaskAdvanceResult(
                                status="waiting",
                                task_id=task.id,
                                turn_ordinal=turns[-1].turn_ordinal if turns else None,
                                observation_ref=latest_observation.observation_ref,
                                reason=resumed.failure_ref,
                            )
                        return self._finish_observation(
                            claim,
                            latest_turn,
                            resumed,
                            f"task-observation:{claim.task_id}:{latest_turn.turn_ordinal}:resume",
                        )
                return TaskAdvanceResult(
                    status="waiting",
                    task_id=task.id,
                    turn_ordinal=turns[-1].turn_ordinal if turns else None,
                    observation_ref=latest_observation.observation_ref,
                    reason=latest_observation.failure_ref,
                )
            turn_ordinal = len(turns) + 1
            context = TaskTurnContext(
                task_id=task.id,
                run_id=task.run_id,
                turn_ordinal=turn_ordinal,
                goal=task.goal,
                success_criteria=tuple(task.success_criteria),
                allowed_tools=tuple(task.allowed_tools),
                previous_observation=_observation_draft(latest_observation),
                tool_snapshot=self._tool_registry,
            )

        # The previous process may have persisted the logical Tool Call and
        # completed its side effect before dying. Reconcile that call before
        # asking the model for another decision.
        if (
            latest_turn is not None
            and latest_turn.output_kind == "tool_call"
            and latest_observation is None
        ):
            recovered = self._recover_inflight_tool(claim, latest_turn)
            if recovered is not None:
                return self._finish_observation(
                    claim,
                    latest_turn,
                    recovered,
                    f"task-observation:{claim.task_id}:{latest_turn.turn_ordinal}",
                )

        output, failure_reason = self._complete_turn(context)
        if failure_reason is not None:
            self._persist_failed_turn(claim, turn_ordinal, failure_reason)
            outcome = self._task_runtime.record_outcome(
                claim,
                TaskExecutionResult(kind="failed", failure_ref=failure_reason),
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                outcome_ref=outcome.outcome_ref,
                reason=failure_reason,
            )
        try:
            normalized = _normalize_task_model_output(output)
        except (InvalidTaskModelOutputError, ValueError) as exc:
            self._persist_failed_turn(claim, turn_ordinal, str(exc))
            outcome = self._task_runtime.record_outcome(
                claim,
                TaskExecutionResult(kind="failed", failure_ref="invalid_model_output"),
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                outcome_ref=outcome.outcome_ref,
                reason="invalid_model_output",
            )

        with self._session_factory() as session:
            current_task = session.get(ResearchTask, claim.task_id)
            current_run = session.get(ResearchRun, claim.run_id)
            turns = session.scalars(
                select(TaskModelTurn)
                .where(TaskModelTurn.task_id == claim.task_id)
                .order_by(TaskModelTurn.turn_ordinal)
            ).all()
            cancelled = current_run is None or current_run.cancel_requested_at is not None
            budget_reason = (
                _task_budget_reason(current_task, turns, current_usage=_output_usage(normalized))
                if current_task is not None
                else "task_missing"
            )
        if cancelled:
            outcome = self._task_runtime.record_outcome(
                claim, TaskExecutionResult(kind="cancelled", failure_ref="run_cancelled")
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                outcome_ref=outcome.outcome_ref,
                reason="run_cancelled",
            )
        if budget_reason is not None:
            self._persist_failed_turn(
                claim,
                turn_ordinal,
                budget_reason,
                usage=_output_usage(normalized),
                output_kind="budget_exceeded",
            )
            outcome = self._task_runtime.record_outcome(
                claim, TaskExecutionResult(kind="failed", failure_ref=budget_reason)
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                outcome_ref=outcome.outcome_ref,
                reason=budget_reason,
            )

        if isinstance(normalized, TaskResultProposal):
            return self._commit_result_proposal(claim, turn_ordinal, normalized)
        return self._execute_tool_call(claim, turn_ordinal, context, normalized)

    def _recover_inflight_tool(
        self, claim: TaskClaim, turn: TaskModelTurn
    ) -> TaskObservationResult | None:
        """Reconcile a tool call whose process died before Observation commit.

        The logical call was written before the adapter ran. Recovery therefore
        never asks the model for another decision. Durable adapters can expose
        ``recover`` to look up the existing operation; local deterministic
        adapters may safely reuse ``execute`` when it is idempotent by the
        stable logical call reference.
        """
        call = _task_tool_call_from_payload(turn.output)
        if call is None:
            return TaskObservationResult(
                status="failed",
                failure_ref="invalid_tool_call",
                error_category="protocol",
            )
        registry = self._tool_registry
        definition = registry.get(call.tool_name) if registry is not None else None
        handler = definition.handler if definition is not None else self._tool_adapter
        if handler is None:
            return TaskObservationResult(
                status="failed",
                failure_ref="tool_adapter_unavailable",
                error_category="execution",
            )
        recover = getattr(handler, "recover", None)
        operation = recover if callable(recover) else handler.execute
        try:
            result = operation(claim, call)
        except Exception as exc:
            category = _failure_category(exc)
            return TaskObservationResult(
                status="failed",
                failure_ref=category,
                error_category=category,
            )
        if not isinstance(result, TaskObservationResult):
            return TaskObservationResult(
                status="failed",
                failure_ref="invalid_tool_observation",
                error_category="protocol",
            )
        return result

    def _complete_turn(self, context: TaskTurnContext) -> tuple[object, str | None]:
        try:
            output = self._model_gateway.complete_task_turn(context)
        except InvalidTaskModelOutputError as exc:
            repair = getattr(self._model_gateway, "repair_task_turn", None)
            if not callable(repair):
                return exc, "invalid_model_output"
            try:
                repaired = repair(context, exc)
            except Exception:
                return exc, "invalid_model_output"
            try:
                _normalize_task_model_output(repaired)
            except InvalidTaskModelOutputError:
                return repaired, "invalid_model_output"
            return repaired, None
        except Exception as exc:
            return exc, _failure_category(exc)
        try:
            _normalize_task_model_output(output)
        except InvalidTaskModelOutputError:
            repair = getattr(self._model_gateway, "repair_task_turn", None)
            if not callable(repair):
                return output, "invalid_model_output"
            try:
                repaired = repair(context, output)
                _normalize_task_model_output(repaired)
            except Exception:
                return output, "invalid_model_output"
            return repaired, None
        return output, None

    def _execute_tool_call(
        self,
        claim: TaskClaim,
        turn_ordinal: int,
        context: TaskTurnContext,
        call: TaskToolCall,
    ) -> TaskAdvanceResult:
        call = _with_call_identity(claim, turn_ordinal, call)
        registry = self._tool_registry
        definition = (
            registry.get(call.tool_name) if registry is not None else None
        )
        turn = self._persist_turn(claim, turn_ordinal, call)
        with self._session_factory() as session:
            task = session.get(ResearchTask, claim.task_id)
            run = session.get(ResearchRun, claim.run_id)
            existing_observation = session.scalar(
                select(TaskObservation)
                .where(
                    TaskObservation.task_id == claim.task_id,
                    TaskObservation.logical_call_ref == call.logical_call_ref,
                )
                .order_by(TaskObservation.created_at, TaskObservation.id)
            )
            tool_call_count = len(
                session.scalars(
                    select(TaskModelTurn).where(
                        TaskModelTurn.task_id == claim.task_id,
                        TaskModelTurn.output_kind == "tool_call",
                    )
                ).all()
            )
        if task is None or run is None:
            raise StaleTaskClaimError("task claim has been fenced")
        if existing_observation is not None:
            return self._replay_observation(claim, turn_ordinal, existing_observation)
        if registry is not None and definition is None:
            return self._finish_observation(
                claim,
                turn,
                TaskObservationResult(
                    status="failed",
                    failure_ref="unknown_tool",
                    error_category="policy",
                    summary="tool is not registered",
                ),
                f"task-observation:{claim.task_id}:{turn_ordinal}",
            )
        if definition is not None:
            assert registry is not None
            schema_error = registry.validate(definition, call.arguments)
            if schema_error is not None:
                return self._finish_observation(
                    claim,
                    turn,
                    TaskObservationResult(
                        status="failed",
                        failure_ref="invalid_arguments",
                        error_category="validation",
                        summary=schema_error,
                    ),
                    f"task-observation:{claim.task_id}:{turn_ordinal}",
                )
            decision = self._tool_policy.check(
                claim,
                task,
                run,
                definition,
                tool_call_count=tool_call_count - 1,
            )
            if not decision.allowed:
                if decision.waiting_reference is not None:
                    return self._finish_observation(
                        claim,
                        turn,
                        TaskObservationResult(
                            status="waiting",
                            failure_ref=decision.reason,
                            error_category="approval",
                            waiting_reference=decision.waiting_reference,
                            summary="tool approval is required",
                        ),
                        f"task-observation:{claim.task_id}:{turn_ordinal}",
                    )
                if decision.reason == "run_cancelled":
                    observation_ref = f"task-observation:{claim.task_id}:{turn_ordinal}"
                    self._persist_observation(
                        claim,
                        turn,
                        TaskObservationResult(
                            status="failed",
                            failure_ref="run_cancelled",
                            error_category="cancelled",
                        ),
                        observation_ref,
                    )
                    outcome = self._task_runtime.record_outcome(
                        claim,
                        TaskExecutionResult(kind="cancelled", failure_ref="run_cancelled"),
                    )
                    return TaskAdvanceResult(
                        status="terminal",
                        task_id=claim.task_id,
                        turn_ordinal=turn_ordinal,
                        observation_ref=observation_ref,
                        outcome_ref=outcome.outcome_ref,
                        reason="run_cancelled",
                    )
                return self._finish_observation(
                    claim,
                    turn,
                    TaskObservationResult(
                        status="failed",
                        failure_ref=decision.reason,
                        error_category="policy",
                        summary="tool call rejected by policy",
                    ),
                    f"task-observation:{claim.task_id}:{turn_ordinal}",
                )
        elif context.allowed_tools and call.tool_name not in context.allowed_tools:
            return self._finish_observation(
                claim,
                turn,
                TaskObservationResult(
                    status="failed",
                    failure_ref="tool_not_allowed",
                    error_category="policy",
                    summary="tool is not in the task allowlist",
                ),
                f"task-observation:{claim.task_id}:{turn_ordinal}",
            )
        handler = definition.handler if definition is not None else self._tool_adapter
        if handler is None:
            return self._finish_observation(
                claim,
                turn,
                TaskObservationResult(
                    status="failed",
                    failure_ref="tool_adapter_unavailable",
                    error_category="execution",
                ),
                f"task-observation:{claim.task_id}:{turn_ordinal}",
            )
        try:
            observation = handler.execute(claim, call)
        except Exception as exc:
            observation = TaskObservationResult(
                status="failed", failure_ref=_failure_category(exc),
                error_category=_failure_category(exc),
            )
        if observation.status not in {"succeeded", "waiting", "failed"}:
            observation = TaskObservationResult(
                status="failed",
                failure_ref="invalid_tool_observation",
                error_category="protocol",
            )
        with self._session_factory() as session:
            run = session.get(ResearchRun, claim.run_id)
            cancelled = run is None or run.cancel_requested_at is not None
        if cancelled:
            outcome = self._task_runtime.record_outcome(
                claim, TaskExecutionResult(kind="cancelled", failure_ref="run_cancelled")
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                outcome_ref=outcome.outcome_ref,
                reason="run_cancelled",
            )
        return self._finish_observation(
            claim,
            turn,
            observation,
            f"task-observation:{claim.task_id}:{turn_ordinal}",
        )

    def _resume_waiting_tool(
        self,
        claim: TaskClaim,
        turn: TaskModelTurn,
        observation: TaskObservation,
    ) -> TaskObservationResult | None:
        if not observation.waiting_reference:
            return None
        call = _task_tool_call_from_payload(turn.output)
        if call is None:
            return None
        definition = (
            self._tool_registry.get(call.tool_name)
            if self._tool_registry is not None
            else None
        )
        handler = definition.handler if definition is not None else self._tool_adapter
        resume = getattr(handler, "resume", None)
        if not callable(resume):
            return None
        try:
            result = resume(claim, call, observation.waiting_reference)
        except Exception as exc:
            return TaskObservationResult(
                status="failed",
                failure_ref=_failure_category(exc),
                error_category=_failure_category(exc),
            )
        if not isinstance(result, TaskObservationResult):
            return TaskObservationResult(
                status="failed",
                failure_ref="invalid_tool_observation",
                error_category="protocol",
            )
        return result

    def _finish_observation(
        self,
        claim: TaskClaim,
        turn: TaskModelTurn,
        observation: TaskObservationResult,
        observation_ref: str,
    ) -> TaskAdvanceResult:
        self._persist_observation(claim, turn, observation, observation_ref)
        if observation.status == "waiting":
            return TaskAdvanceResult(
                status="waiting",
                task_id=claim.task_id,
                turn_ordinal=turn.turn_ordinal,
                observation_ref=observation_ref,
                reason=observation.failure_ref,
            )
        if observation.status == "failed":
            outcome = self._task_runtime.record_outcome(
                claim,
                TaskExecutionResult(kind="failed", failure_ref=observation.failure_ref),
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn.turn_ordinal,
                observation_ref=observation_ref,
                outcome_ref=outcome.outcome_ref,
                reason=observation.failure_ref,
            )
        if observation.evidence_gain:
            return TaskAdvanceResult(
                status="runnable",
                task_id=claim.task_id,
                turn_ordinal=turn.turn_ordinal,
                observation_ref=observation_ref,
            )
        with self._session_factory() as session:
            no_gain_streak = _no_evidence_gain_streak(
                session, claim.task_id, self._no_evidence_gain_limit
            )
        if no_gain_streak >= self._no_evidence_gain_limit:
            outcome = self._task_runtime.record_outcome(
                claim,
                TaskExecutionResult(kind="failed", failure_ref="no_evidence_gain"),
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn.turn_ordinal,
                observation_ref=observation_ref,
                outcome_ref=outcome.outcome_ref,
                reason="no_evidence_gain",
            )
        return TaskAdvanceResult(
            status="runnable",
            task_id=claim.task_id,
            turn_ordinal=turn.turn_ordinal,
            observation_ref=observation_ref,
        )

    def _replay_observation(
        self, claim: TaskClaim, turn_ordinal: int, observation: TaskObservation
    ) -> TaskAdvanceResult:
        if observation.status == "waiting":
            return TaskAdvanceResult(
                status="waiting",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                observation_ref=observation.observation_ref,
                reason=observation.failure_ref,
            )
        if observation.status == "failed":
            outcome = self._task_runtime.record_outcome(
                claim,
                TaskExecutionResult(kind="failed", failure_ref=observation.failure_ref),
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                observation_ref=observation.observation_ref,
                outcome_ref=outcome.outcome_ref,
                reason=observation.failure_ref,
            )
        if not observation.evidence_gain:
            with self._session_factory() as session:
                replay_count = len(
                    session.scalars(
                        select(TaskModelTurn).where(
                            TaskModelTurn.task_id == claim.task_id,
                            TaskModelTurn.logical_call_ref == observation.logical_call_ref,
                            TaskModelTurn.output_kind == "tool_call",
                        )
                    ).all()
                )
            if replay_count >= self._no_evidence_gain_limit:
                outcome = self._task_runtime.record_outcome(
                    claim,
                    TaskExecutionResult(kind="failed", failure_ref="no_evidence_gain"),
                )
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=claim.task_id,
                    turn_ordinal=turn_ordinal,
                    observation_ref=observation.observation_ref,
                    outcome_ref=outcome.outcome_ref,
                    reason="no_evidence_gain",
                )
        return TaskAdvanceResult(
            status="runnable",
            task_id=claim.task_id,
            turn_ordinal=turn_ordinal,
            observation_ref=observation.observation_ref,
        )

    def _commit_result_proposal(
        self, claim: TaskClaim, turn_ordinal: int, proposal: TaskResultProposal
    ) -> TaskAdvanceResult:
        with self._session_factory.begin() as session:
            task, _run, existing = self._active_claim_state(session, claim)
            if existing is not None:
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=task.id,
                    turn_ordinal=turn_ordinal,
                    outcome_ref=existing.outcome_ref,
                )
            turn = TaskModelTurn(
                workspace_id=task.workspace_id,
                run_id=task.run_id,
                task_id=task.id,
                fencing_epoch=claim.fencing_epoch,
                turn_ordinal=turn_ordinal,
                output_kind="result_proposal",
                output=_proposal_payload(proposal),
                usage=proposal.usage,
                provider_reference=proposal.provider_reference,
                tool_snapshot=self._tool_registry.as_dict() if self._tool_registry else None,
            )
            session.add(turn)
            session.flush()
            criteria = set(task.success_criteria)
            covered = set(proposal.covered_criteria)
            valid = (
                bool(proposal.result_reference and proposal.evidence_refs)
                and criteria <= covered
            )
            rejection_reason = None if valid else _proposal_rejection(task, proposal)
            session.add(
                TaskResultProposalRecord(
                    workspace_id=task.workspace_id,
                    run_id=task.run_id,
                    task_id=task.id,
                    turn_id=turn.id,
                    result_reference=proposal.result_reference,
                    evidence_refs=list(proposal.evidence_refs),
                    covered_criteria=list(proposal.covered_criteria),
                    valid=valid,
                    rejection_reason=rejection_reason,
                )
            )
        outcome = self._task_runtime.record_outcome(
            claim,
            TaskExecutionResult(
                kind="completed" if valid else "partial",
                result_reference=proposal.result_reference,
                evidence_refs=proposal.evidence_refs,
                failure_ref=rejection_reason,
            ),
        )
        return TaskAdvanceResult(
            status="terminal",
            task_id=claim.task_id,
            turn_ordinal=turn_ordinal,
            outcome_ref=outcome.outcome_ref,
            reason=rejection_reason,
        )

    def _persist_turn(
        self, claim: TaskClaim, turn_ordinal: int, output: TaskToolCall
    ) -> TaskModelTurn:
        with self._session_factory.begin() as session:
            task, _run, existing = self._active_claim_state(session, claim)
            if existing is not None:
                raise StaleTaskClaimError("task outcome already committed")
            turn = TaskModelTurn(
                workspace_id=task.workspace_id,
                run_id=task.run_id,
                task_id=task.id,
                fencing_epoch=claim.fencing_epoch,
                turn_ordinal=turn_ordinal,
                output_kind="tool_call",
                output=_tool_call_payload(output),
                usage=output.usage,
                provider_reference=output.provider_reference,
                logical_call_ref=output.logical_call_ref,
                parameters_hash=output.parameters_hash,
                safe_summary=output.safe_summary,
                tool_snapshot=self._tool_registry.as_dict() if self._tool_registry else None,
            )
            session.add(turn)
            session.flush()
            return turn

    def _persist_observation(
        self,
        claim: TaskClaim,
        turn: TaskModelTurn,
        observation: TaskObservationResult,
        observation_ref: str,
    ) -> None:
        with self._session_factory.begin() as session:
            task, _run, existing = self._active_claim_state(session, claim)
            if existing is not None:
                return
            session.add(
                TaskObservation(
                    workspace_id=task.workspace_id,
                    run_id=task.run_id,
                    task_id=task.id,
                    turn_id=turn.id,
                    observation_ref=observation_ref,
                    status=observation.status,
                    result_reference=observation.result_reference,
                    evidence_refs=list(observation.evidence_refs),
                    file_refs=list(observation.file_refs),
                    evidence_gain=observation.evidence_gain,
                    failure_ref=observation.failure_ref,
                    logical_call_ref=turn.logical_call_ref,
                    summary=_bounded_summary(observation.summary),
                    error_category=observation.error_category,
                    waiting_reference=observation.waiting_reference,
                )
            )

    def _persist_failed_turn(
        self,
        claim: TaskClaim,
        turn_ordinal: int,
        reason: str,
        *,
        usage: dict[str, int | float] | None = None,
        output_kind: str = "invalid",
    ) -> None:
        with self._session_factory.begin() as session:
            task, _run, existing = self._active_claim_state(session, claim)
            if existing is not None:
                return
            session.add(
                TaskModelTurn(
                    workspace_id=task.workspace_id,
                    run_id=task.run_id,
                    task_id=task.id,
                    fencing_epoch=claim.fencing_epoch,
                    turn_ordinal=turn_ordinal,
                    status="failed",
                    output_kind=output_kind,
                    usage=usage,
                    failure_reason=reason[:200],
                )
            )

    @staticmethod
    def _active_claim_state(
        session: Session, claim: TaskClaim
    ) -> tuple[ResearchTask, ResearchRun, TaskOutcome | None]:
        task = session.scalar(
            select(ResearchTask).where(ResearchTask.id == claim.task_id).with_for_update()
        )
        run = session.get(ResearchRun, claim.run_id)
        if task is None or run is None:
            raise StaleTaskClaimError("task claim has been fenced")
        existing = session.scalar(select(TaskOutcome).where(TaskOutcome.task_id == task.id))
        if existing is not None:
            return task, run, existing
        if (
            task.run_id != claim.run_id
            or task.lease_owner != claim.lease_owner
            or task.fencing_epoch != claim.fencing_epoch
            or task.lease_expires_at is None
            or _as_utc(task.lease_expires_at) < datetime.now(UTC)
        ):
            raise StaleTaskClaimError("task claim has been fenced")
        return task, run, None


def _with_call_identity(
    claim: TaskClaim, turn_ordinal: int, call: TaskToolCall
) -> TaskToolCall:
    canonical = json.dumps(
        call.arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    parameters_hash = hashlib.sha256(canonical.encode()).hexdigest()
    logical_call_ref = f"tool-call:{claim.task_id}:{call.tool_name}:{parameters_hash[:16]}"
    safe_summary = f"{call.tool_name}({canonical[:900]})"
    return replace(
        call,
        logical_call_ref=logical_call_ref,
        parameters_hash=parameters_hash,
        safe_summary=safe_summary,
    )


def _task_tool_call_from_payload(
    payload: dict[str, object] | None,
) -> TaskToolCall | None:
    if not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name")
    arguments = payload.get("arguments")
    if not isinstance(tool_name, str) or not isinstance(arguments, dict):
        return None
    logical_call_ref = payload.get("logical_call_ref")
    parameters_hash = payload.get("parameters_hash")
    safe_summary = payload.get("safe_summary")
    return TaskToolCall(
        tool_name=tool_name,
        arguments=arguments,
        logical_call_ref=logical_call_ref if isinstance(logical_call_ref, str) else None,
        parameters_hash=parameters_hash if isinstance(parameters_hash, str) else None,
        safe_summary=safe_summary if isinstance(safe_summary, str) else None,
    )


def _bounded_summary(summary: str | None) -> str | None:
    if summary is None:
        return None
    return summary[:2000]


def _validate_tool_schema(
    schema: dict[str, object], value: object, path: str
) -> str | None:
    schema_type = schema.get("type")
    if schema_type is not None and not _matches_schema_type(schema_type, value):
        return f"{path} must be {schema_type}"
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path} must be one of the allowed values"
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    return f"{path}.{key} is required"
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child in value.items():
                if key not in properties:
                    if schema.get("additionalProperties") is False:
                        return f"{path}.{key} is not allowed"
                    continue
                child_schema = properties[key]
                if isinstance(child_schema, dict):
                    error = _validate_tool_schema(child_schema, child, f"{path}.{key}")
                    if error is not None:
                        return error
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        assert isinstance(item_schema, dict)
        for index, child in enumerate(value):
            error = _validate_tool_schema(item_schema, child, f"{path}[{index}]")
            if error is not None:
                return error
    return None


def _matches_schema_type(schema_type: object, value: object) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return True


def _normalize_task_model_output(output: object) -> TaskModelOutput:
    if isinstance(output, TaskToolCall):
        if not output.tool_name or not isinstance(output.arguments, dict):
            raise InvalidTaskModelOutputError("tool_call contract invalid")
        try:
            json.dumps(output.arguments, ensure_ascii=True, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise InvalidTaskModelOutputError("tool_call arguments are not JSON") from exc
        return TaskToolCall(
            tool_name=output.tool_name,
            arguments=output.arguments,
            provider_reference=output.provider_reference,
            usage=_normalize_usage(output.usage),
            logical_call_ref=output.logical_call_ref,
            parameters_hash=output.parameters_hash,
            safe_summary=output.safe_summary,
        )
    if isinstance(output, TaskResultProposal):
        evidence_refs = tuple(output.evidence_refs)
        covered_criteria = tuple(output.covered_criteria)
        if not all(isinstance(value, str) and value for value in evidence_refs):
            raise InvalidTaskModelOutputError("result proposal evidence_refs invalid")
        if not all(isinstance(value, str) and value for value in covered_criteria):
            raise InvalidTaskModelOutputError("result proposal covered_criteria invalid")
        return TaskResultProposal(
            result_reference=output.result_reference,
            evidence_refs=evidence_refs,
            covered_criteria=covered_criteria,
            provider_reference=output.provider_reference,
            usage=_normalize_usage(output.usage),
        )
    raise InvalidTaskModelOutputError("unsupported task model output")


def _tool_call_payload(call: TaskToolCall) -> dict[str, object]:
    return {
        "tool_name": call.tool_name,
        "arguments": call.arguments,
        "logical_call_ref": call.logical_call_ref,
        "parameters_hash": call.parameters_hash,
        "safe_summary": call.safe_summary,
    }


def _proposal_payload(proposal: TaskResultProposal) -> dict[str, object]:
    return {
        "result_reference": proposal.result_reference,
        "evidence_refs": list(proposal.evidence_refs),
        "covered_criteria": list(proposal.covered_criteria),
    }


def _normalize_usage(
    usage: dict[str, int | float] | None,
) -> dict[str, int | float] | None:
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise InvalidTaskModelOutputError("model usage invalid")
    normalized: dict[str, int | float] = {}
    for key, value in usage.items():
        if not isinstance(key, str) or not isinstance(value, (int, float)):
            raise InvalidTaskModelOutputError("model usage invalid")
        if isinstance(value, bool) or value < 0:
            raise InvalidTaskModelOutputError("model usage invalid")
        normalized[key] = value
    return normalized


def _proposal_rejection(task: ResearchTask, proposal: TaskResultProposal) -> str:
    if not proposal.result_reference:
        return "result_reference_missing"
    if not proposal.evidence_refs:
        return "evidence_refs_missing"
    missing = set(task.success_criteria) - set(proposal.covered_criteria)
    return "success_criteria_missing" if missing else "result_contract_invalid"


def _observation_draft(observation: TaskObservation | None) -> TaskObservationResult | None:
    if observation is None:
        return None
    return TaskObservationResult(
        status=observation.status,
        result_reference=observation.result_reference,
        evidence_refs=tuple(observation.evidence_refs),
        evidence_gain=observation.evidence_gain,
        failure_ref=observation.failure_ref,
        summary=observation.summary,
        error_category=observation.error_category,
        waiting_reference=observation.waiting_reference,
    )


def _no_evidence_gain_streak(session: Session, task_id: UUID, limit: int) -> int:
    observations = session.scalars(
        select(TaskObservation)
        .where(TaskObservation.task_id == task_id)
        .order_by(TaskObservation.created_at.desc(), TaskObservation.id.desc())
        .limit(limit)
    ).all()
    streak = 0
    for observation in observations:
        if observation.status != "succeeded" or observation.evidence_gain:
            break
        streak += 1
    return streak


def _output_usage(output: TaskModelOutput) -> dict[str, int | float] | None:
    return output.usage


def _task_budget_reason(
    task: ResearchTask | None,
    turns: Sequence[TaskModelTurn],
    *,
    current_usage: dict[str, int | float] | None = None,
) -> str | None:
    if task is None:
        return "task_missing"
    usage = [turn.usage for turn in turns]
    if current_usage is not None:
        usage.append(current_usage)
    total_tokens = sum(
        int(item.get("total_tokens", 0))
        for item in usage
        if item is not None
    )
    if total_tokens >= task.token_budget:
        return "token_budget_exhausted"
    cost_budget = task.local_budget.get("cost_budget_micros")
    if isinstance(cost_budget, int):
        total_cost = sum(
            int(float(item.get("cost_usd", 0)) * 1_000_000)
            for item in usage
            if item is not None
        )
        if total_cost >= cost_budget:
            return "cost_budget_exhausted"
    deadline = _as_utc(task.created_at) + timedelta(seconds=task.time_budget_seconds)
    if datetime.now(UTC) >= deadline:
        return "deadline_exhausted"
    return None


def _failure_category(error: Exception) -> str:
    name = error.__class__.__name__.lower()
    if isinstance(error, TimeoutError) or "timeout" in name:
        return "provider_timeout"
    if getattr(error, "status_code", None) == 429:
        return "provider_rate_limited"
    return "provider_error"


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
