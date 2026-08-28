"""Durable, fenced Python Sandbox Tool Job boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import (
    EvidenceSpan,
    ResearchTask,
    SandboxAttempt,
    SandboxJob,
    SandboxObservation,
    utc_now,
)
from deep_researcher.research_file_space import ResearchFileRef, ResearchFileStore

if TYPE_CHECKING:
    from deep_researcher.task_runtime import (
        TaskClaim,
        TaskObservationResult,
        TaskToolCall,
        ToolDefinition,
    )

ALLOWED_PURPOSES = frozenset({"inspect_data", "transform_artifact", "derive_evidence"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "timed_out", "cancelled"})
NON_RETRYABLE_ERRORS = frozenset(
    {"code_error", "permission_denied", "output_violation", "cancelled", "deadline"}
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class SandboxExecutionOutput:
    status: str
    stdout: str = ""
    stderr: str = ""
    file_refs: tuple[dict[str, str], ...] = ()
    result_reference: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    retryable: bool = False


class SandboxExecutor(Protocol):
    """替换 Docker、microVM 或远端 provider 的物理执行器接口。"""

    def execute(
        self,
        *,
        job: SandboxJob,
        attempt: SandboxAttempt,
        input_refs: tuple[ResearchFileRef | dict[str, str], ...],
    ) -> SandboxExecutionOutput:
        """Execute one fenced Attempt without receiving host paths or runtime flags."""

    def cancel(self, attempt: SandboxAttempt) -> bool:
        """Request termination of a physical Attempt."""


class SandboxJobError(ValueError):
    """Structured request validation failure."""


class SandboxJobService:
    """Owns durable submission, leasing, cancellation and idempotent publication."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        file_store: ResearchFileStore,
        *,
        lease_seconds: int = 30,
        max_timeout_seconds: int = 300,
        preview_bytes: int = 100_000,
    ) -> None:
        self._session_factory = session_factory
        self._file_store = file_store
        self._lease_seconds = lease_seconds
        self._max_timeout_seconds = max_timeout_seconds
        self._preview_bytes = preview_bytes

    def submit(
        self,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID,
        invocation_key: str,
        purpose: str,
        code: str,
        input_refs: tuple[ResearchFileRef | str | dict[str, object], ...] = (),
        timeout_seconds: int = 30,
        max_attempts: int = 3,
    ) -> SandboxJob:
        self._validate_request(purpose, code, invocation_key, timeout_seconds, max_attempts)
        if timeout_seconds > self._max_timeout_seconds:
            raise SandboxJobError("sandbox timeout exceeds deployment policy")
        parsed_refs: list[ResearchFileRef | dict[str, str]] = []
        for ref in input_refs:
            if isinstance(ref, dict) and ref.get("kind") == "evidence_span":
                try:
                    evidence_id = UUID(str(ref["id"]))
                except (KeyError, ValueError) as exc:
                    raise SandboxJobError("invalid evidence span reference") from exc
                with self._session_factory() as session:
                    evidence = session.scalar(
                        select(EvidenceSpan).where(
                            EvidenceSpan.id == evidence_id,
                            EvidenceSpan.workspace_id == workspace_id,
                            EvidenceSpan.run_id == run_id,
                        )
                    )
                if evidence is None:
                    raise SandboxJobError("evidence span is not available to this Run")
                parsed_refs.append({"kind": "evidence_span", "id": str(evidence_id)})
                continue
            parsed = ResearchFileRef.parse(ref)
            if parsed.kind not in {"source", "work", "artifact"}:
                raise SandboxJobError("sandbox inputs must be immutable file revisions")
            self._file_store.read(
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                ref=parsed,
                shared_refs=(parsed,),
            )
            parsed_refs.append(parsed)
        canonical = {
            "purpose": purpose,
            "code": code,
            "input_refs": [
                ref if isinstance(ref, dict) else ref.as_dict() for ref in parsed_refs
            ],
            "timeout_seconds": timeout_seconds,
        }
        parameters_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self._session_factory.begin() as session:
            task = session.scalar(
                select(ResearchTask).where(
                    ResearchTask.id == task_id,
                    ResearchTask.workspace_id == workspace_id,
                    ResearchTask.run_id == run_id,
                )
            )
            if task is None:
                raise SandboxJobError("sandbox task does not exist")
            existing = session.scalar(
                select(SandboxJob).where(SandboxJob.invocation_key == invocation_key)
            )
            if existing is not None:
                if existing.parameters_hash != parameters_hash:
                    raise SandboxJobError("invocation key was reused with different parameters")
                return existing
            job = SandboxJob(
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                invocation_key=invocation_key,
                parameters_hash=parameters_hash,
                purpose=purpose,
                code=code,
                input_refs=[
                    ref if isinstance(ref, dict) else ref.as_dict() for ref in parsed_refs
                ],
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                deadline_at=utc_now() + timedelta(seconds=timeout_seconds),
            )
            session.add(job)
            session.flush()
            return job

    def claim(self, *, worker_id: str) -> tuple[SandboxJob, SandboxAttempt] | None:
        now = utc_now()
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SandboxJob)
                .where(
                    SandboxJob.status.in_({"queued", "retry_wait", "running"}),
                    SandboxJob.cancel_requested_at.is_(None),
                    or_(
                        SandboxJob.status.in_({"queued", "retry_wait"}),
                        SandboxJob.lease_expires_at < now,
                    ),
                )
                .order_by(SandboxJob.created_at)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            deadline = _utc(job.deadline_at)
            if deadline is not None and deadline <= now:
                job.status = "timed_out"
                job.completed_at = now
                return None
            if job.attempt_count >= job.max_attempts:
                job.status = "failed"
                job.error_category = "attempt_limit"
                job.completed_at = now
                return None
            job.attempt_count += 1
            job.status = "running"
            job.started_at = job.started_at or now
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            attempt = SandboxAttempt(
                job_id=job.id,
                attempt_number=job.attempt_count,
                lease_owner=worker_id,
                lease_expires_at=job.lease_expires_at,
                input_manifest=list(job.input_refs),
                status="leased",
            )
            session.add(attempt)
            session.flush()
            return job, attempt

    def publish(
        self,
        *,
        job_id: UUID,
        attempt_id: UUID,
        worker_id: str,
        output: SandboxExecutionOutput,
    ) -> SandboxObservation:
        if output.status not in TERMINAL_STATUSES:
            raise SandboxJobError("sandbox output must be terminal")
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SandboxJob).where(SandboxJob.id == job_id).with_for_update()
            )
            attempt = session.get(SandboxAttempt, attempt_id)
            if job is None or attempt is None or attempt.job_id != job.id:
                raise SandboxJobError("sandbox job or attempt does not exist")
            existing = session.scalar(
                select(SandboxObservation).where(SandboxObservation.job_id == job.id)
            )
            if existing is not None:
                return existing
            attempt_deadline = _utc(attempt.lease_expires_at)
            if (
                job.lease_owner != worker_id
                or attempt.lease_owner != worker_id
                or (attempt_deadline is not None and attempt_deadline <= utc_now())
            ):
                raise SandboxJobError("sandbox lease was fenced")
            if job.cancel_requested_at is not None:
                output = SandboxExecutionOutput(
                    status="cancelled",
                    stderr="sandbox execution was cancelled",
                    error_category="cancelled",
                )
            deadline = _utc(job.deadline_at)
            if deadline is not None and deadline <= utc_now():
                output = SandboxExecutionOutput(
                    status="timed_out",
                    stderr="sandbox deadline exceeded",
                    error_category="deadline",
                )
            now = utc_now()
            attempt.status = output.status
            attempt.completed_at = now
            attempt.retryable = (
                output.retryable and output.error_category not in NON_RETRYABLE_ERRORS
            )
            attempt.error_category = output.error_category
            attempt.error_message = output.error_message
            job.status = output.status
            job.error_category = output.error_category
            job.error_message = output.error_message
            job.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            observation = SandboxObservation(
                job_id=job.id,
                attempt_id=attempt.id,
                status=output.status,
                attempt_count=job.attempt_count,
                stdout_preview=output.stdout[-self._preview_bytes :],
                stderr_preview=output.stderr[-self._preview_bytes :],
                file_refs=list(output.file_refs),
                result_reference=output.result_reference,
                error_category=output.error_category,
                error_message=output.error_message,
            )
            session.add(observation)
            session.flush()
            return observation

    def heartbeat(self, *, job_id: UUID, attempt_id: UUID, worker_id: str) -> bool:
        """Extend only the currently fenced Attempt lease."""
        now = utc_now()
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SandboxJob).where(SandboxJob.id == job_id).with_for_update()
            )
            attempt = session.get(SandboxAttempt, attempt_id)
            if (
                job is None
                or attempt is None
                or attempt.job_id != job.id
                or job.lease_owner != worker_id
                or attempt.lease_owner != worker_id
                or job.cancel_requested_at is not None
            ):
                return False
            job.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            attempt.lease_expires_at = job.lease_expires_at
            attempt.heartbeat_at = now
            return True

    def get(self, job_id: UUID) -> SandboxJob | None:
        with self._session_factory() as session:
            return session.get(SandboxJob, job_id)

    def run_once(self, *, worker_id: str, executor: SandboxExecutor) -> bool:
        """Claim and execute one Job; retryable infrastructure failures remain durable."""
        claimed = self.claim(worker_id=worker_id)
        if claimed is None:
            return False
        job, attempt = claimed
        refs: tuple[ResearchFileRef | dict[str, str], ...] = tuple(
            ResearchFileRef.parse(ref)
            if ref.get("kind") != "evidence_span"
            else ref
            for ref in job.input_refs
        )
        try:
            output = executor.execute(job=job, attempt=attempt, input_refs=refs)
        except Exception as exc:
            output = SandboxExecutionOutput(
                status="failed",
                error_category="infrastructure",
                error_message=str(exc),
                retryable=True,
            )
        if output.retryable and job.attempt_count < job.max_attempts:
            self._schedule_retry(job.id, attempt.id, worker_id, output)
            return True
        self.publish(
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id=worker_id,
            output=output,
        )
        return True

    def _schedule_retry(
        self,
        job_id: UUID,
        attempt_id: UUID,
        worker_id: str,
        output: SandboxExecutionOutput,
    ) -> None:
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SandboxJob).where(SandboxJob.id == job_id).with_for_update()
            )
            attempt = session.get(SandboxAttempt, attempt_id)
            if (
                job is None
                or attempt is None
                or job.lease_owner != worker_id
                or attempt.lease_owner != worker_id
            ):
                raise SandboxJobError("sandbox lease was fenced")
            attempt.status = "retry_wait"
            attempt.retryable = True
            attempt.error_category = output.error_category
            attempt.error_message = output.error_message
            job.status = "retry_wait"
            job.error_category = output.error_category
            job.error_message = output.error_message
            job.lease_owner = None
            job.lease_expires_at = None

    def cancel(self, job_id: UUID) -> SandboxJob:
        with self._session_factory.begin() as session:
            job = session.scalar(
                select(SandboxJob).where(SandboxJob.id == job_id).with_for_update()
            )
            if job is None:
                raise SandboxJobError("sandbox job does not exist")
            if job.status in TERMINAL_STATUSES:
                return job
            job.cancel_requested_at = utc_now()
            if job.status in {"queued", "retry_wait"}:
                job.status = "cancelled"
                job.completed_at = utc_now()
            else:
                job.status = "cancel_requested"
            return job

    @staticmethod
    def _validate_request(
        purpose: str, code: str, invocation_key: str, timeout_seconds: int, max_attempts: int
    ) -> None:
        if purpose not in ALLOWED_PURPOSES:
            raise SandboxJobError("unsupported sandbox purpose")
        if not code.strip():
            raise SandboxJobError("sandbox code is required")
        if not invocation_key or len(invocation_key) > 300:
            raise SandboxJobError("bounded invocation key is required")
        if timeout_seconds < 1 or timeout_seconds > 300:
            raise SandboxJobError("sandbox timeout exceeds policy")
        if max_attempts < 1 or max_attempts > 3:
            raise SandboxJobError("sandbox attempt limit exceeds policy")


class SandboxJobWorker:
    """Polls the durable Sandbox Job queue from the dedicated Worker process."""

    def __init__(
        self,
        service: SandboxJobService,
        executor: SandboxExecutor,
        *,
        worker_id: str,
    ) -> None:
        self._service = service
        self._executor = executor
        self._worker_id = worker_id

    def run_once(self) -> bool:
        return self._service.run_once(
            worker_id=self._worker_id,
            executor=self._executor,
        )


class SandboxToolAdapter:
    """Task Tool adapter that queues a Job and returns a durable wait reference."""

    def __init__(self, service: SandboxJobService) -> None:
        self._service = service

    def execute(self, claim: TaskClaim, call: TaskToolCall) -> TaskObservationResult:
        from deep_researcher.task_runtime import TaskObservationResult

        arguments = call.arguments
        refs = cast(
            tuple[ResearchFileRef | str | dict[str, object], ...],
            arguments.get("input_refs", ()),
        )
        job = self._service.submit(
            workspace_id=self._workspace_id(claim),
            run_id=claim.run_id,
            task_id=claim.task_id,
            invocation_key=call.logical_call_ref or f"sandbox:{claim.task_id}:{call.tool_name}",
            purpose=str(arguments["purpose"]),
            code=str(arguments["code"]),
            input_refs=refs,
            timeout_seconds=int(cast(int, arguments.get("timeout_seconds", 30))),
        )
        return TaskObservationResult(
            status="waiting",
            waiting_reference=f"sandbox-job:{job.id}",
            result_reference=f"sandbox-job://{job.id}",
            summary="Sandbox Job 已排队，等待专用 Worker 完成",
        )

    def _workspace_id(self, claim: TaskClaim) -> UUID:
        with self._service._session_factory() as session:
            task = session.get(ResearchTask, claim.task_id)
            if task is None or task.run_id != claim.run_id:
                raise SandboxJobError("sandbox task claim is not available")
            return task.workspace_id


def python_execute_tool_definition(service: SandboxJobService) -> ToolDefinition:
    """Return the only model-facing Python Sandbox Tool contract."""
    from deep_researcher.task_runtime import ToolDefinition

    return ToolDefinition(
        name="python_execute",
        input_schema={
            "type": "object",
            "required": ["purpose", "code"],
            "properties": {
                "purpose": {
                    "type": "string",
                    "enum": ["inspect_data", "transform_artifact", "derive_evidence"],
                },
                "code": {"type": "string", "minLength": 1},
                "input_refs": {"type": "array"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "additionalProperties": False,
        },
        result_contract="durable sandbox observation",
        handler=SandboxToolAdapter(service),
    )
