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
    evidence_gain: bool = False
    failure_ref: str | None = None


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

    def complete_task_turn(self, context: TaskTurnContext) -> object:
        del context
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
            if (
                latest_turn is not None
                and latest_turn.output_kind == "tool_call"
                and latest_observation is None
            ):
                reason = "tool_observation_lost"
                outcome = self._task_runtime.record_outcome(
                    claim, TaskExecutionResult(kind="failed", failure_ref=reason)
                )
                return TaskAdvanceResult(
                    status="terminal",
                    task_id=task.id,
                    turn_ordinal=latest_turn.turn_ordinal,
                    outcome_ref=outcome.outcome_ref,
                    reason=reason,
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
            )

        output, repair_failed = self._complete_turn(context)
        if repair_failed:
            self._persist_failed_turn(claim, turn_ordinal, "invalid_model_output")
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

    def _complete_turn(self, context: TaskTurnContext) -> tuple[object, bool]:
        try:
            output = self._model_gateway.complete_task_turn(context)
        except InvalidTaskModelOutputError as exc:
            repair = getattr(self._model_gateway, "repair_task_turn", None)
            if not callable(repair):
                return exc, True
            try:
                repaired = repair(context, exc)
            except Exception:
                return exc, True
            try:
                _normalize_task_model_output(repaired)
            except InvalidTaskModelOutputError:
                return repaired, True
            return repaired, False
        except Exception as exc:
            return exc, True
        try:
            _normalize_task_model_output(output)
        except InvalidTaskModelOutputError:
            repair = getattr(self._model_gateway, "repair_task_turn", None)
            if not callable(repair):
                return output, True
            try:
                repaired = repair(context, output)
                _normalize_task_model_output(repaired)
            except Exception:
                return output, True
            return repaired, False
        return output, False

    def _execute_tool_call(
        self,
        claim: TaskClaim,
        turn_ordinal: int,
        context: TaskTurnContext,
        call: TaskToolCall,
    ) -> TaskAdvanceResult:
        if context.allowed_tools and call.tool_name not in context.allowed_tools:
            self._persist_failed_turn(claim, turn_ordinal, "tool_not_allowed")
            outcome = self._task_runtime.record_outcome(
                claim, TaskExecutionResult(kind="failed", failure_ref="tool_not_allowed")
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                outcome_ref=outcome.outcome_ref,
                reason="tool_not_allowed",
            )
        if self._tool_adapter is None:
            self._persist_failed_turn(claim, turn_ordinal, "tool_adapter_unavailable")
            outcome = self._task_runtime.record_outcome(
                claim,
                TaskExecutionResult(kind="failed", failure_ref="tool_adapter_unavailable"),
            )
            return TaskAdvanceResult(
                status="terminal",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
                outcome_ref=outcome.outcome_ref,
                reason="tool_adapter_unavailable",
            )
        turn = self._persist_turn(claim, turn_ordinal, call)
        try:
            observation = self._tool_adapter.execute(claim, call)
        except Exception as exc:
            observation = TaskObservationResult(
                status="failed",
                failure_ref=_failure_category(exc),
            )
        if observation.status not in {"succeeded", "waiting", "failed"}:
            observation = TaskObservationResult(
                status="failed", failure_ref="invalid_tool_observation"
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
        observation_ref = f"task-observation:{claim.task_id}:{turn_ordinal}"
        self._persist_observation(claim, turn, observation, observation_ref)
        if observation.status == "waiting":
            return TaskAdvanceResult(
                status="waiting",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
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
                turn_ordinal=turn_ordinal,
                observation_ref=observation_ref,
                outcome_ref=outcome.outcome_ref,
                reason=observation.failure_ref,
            )
        if observation.evidence_gain:
            return TaskAdvanceResult(
                status="runnable",
                task_id=claim.task_id,
                turn_ordinal=turn_ordinal,
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
                turn_ordinal=turn_ordinal,
                observation_ref=observation_ref,
                outcome_ref=outcome.outcome_ref,
                reason="no_evidence_gain",
            )
        return TaskAdvanceResult(
            status="runnable",
            task_id=claim.task_id,
            turn_ordinal=turn_ordinal,
            observation_ref=observation_ref,
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
                    evidence_gain=observation.evidence_gain,
                    failure_ref=observation.failure_ref,
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
    return {"tool_name": call.tool_name, "arguments": call.arguments}


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
