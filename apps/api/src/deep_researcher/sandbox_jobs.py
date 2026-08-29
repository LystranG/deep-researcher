"""Durable, fenced Python Sandbox Tool Job boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from sqlalchemy import desc, or_, select
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
from deep_researcher.sandbox import DockerSandbox, SandboxInputMount, SandboxRequest

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
    artifact_contents: tuple[tuple[str, str], ...] = ()
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
        observation_notifier: Callable[[UUID, UUID], object] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._file_store = file_store
        self._lease_seconds = lease_seconds
        self._max_timeout_seconds = max_timeout_seconds
        self._preview_bytes = preview_bytes
        self._observation_notifier = observation_notifier

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
                self._publish_unclaimed_terminal(
                    session,
                    job,
                    worker_id=worker_id,
                    status="timed_out",
                    error_category="deadline",
                    error_message="sandbox deadline exceeded before execution",
                    now=now,
                )
                return None
            if job.attempt_count >= job.max_attempts:
                self._publish_unclaimed_terminal(
                    session,
                    job,
                    worker_id=worker_id,
                    status="failed",
                    error_category="attempt_limit",
                    error_message="sandbox attempt limit exceeded",
                    now=now,
                )
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

    def _publish_unclaimed_terminal(
        self,
        session: Session,
        job: SandboxJob,
        *,
        worker_id: str,
        status: str,
        error_category: str,
        error_message: str,
        now: datetime,
    ) -> None:
        """Persist a terminal Observation when no physical execution starts."""
        existing = session.scalar(
            select(SandboxObservation).where(SandboxObservation.job_id == job.id)
        )
        if existing is not None:
            return
        attempt = session.scalar(
            select(SandboxAttempt)
            .where(SandboxAttempt.job_id == job.id)
            .order_by(desc(SandboxAttempt.attempt_number))
        )
        if attempt is None:
            attempt = SandboxAttempt(
                job_id=job.id,
                attempt_number=job.attempt_count,
                lease_owner=worker_id,
                lease_expires_at=now,
                input_manifest=list(job.input_refs),
                status=status,
            )
            session.add(attempt)
            session.flush()
        attempt.status = status
        attempt.completed_at = now
        attempt.error_category = error_category
        attempt.error_message = error_message
        job.status = status
        job.error_category = error_category
        job.error_message = error_message
        job.completed_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        session.add(
            SandboxObservation(
                job_id=job.id,
                attempt_id=attempt.id,
                status=status,
                attempt_count=job.attempt_count,
                stderr_preview=error_message,
                error_category=error_category,
                error_message=error_message,
            )
        )

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
            run_id = job.run_id
            task_id = job.task_id
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
            file_refs = list(output.file_refs)
            if output.status == "completed" and output.artifact_contents:
                for name, content in output.artifact_contents:
                    artifact = self._file_store.ingest_artifact(
                        workspace_id=job.workspace_id,
                        run_id=job.run_id,
                        task_id=job.task_id,
                        name=name,
                        content=content,
                        idempotency_key=f"sandbox:{job.id}:attempt:{attempt.id}:{name}",
                    )
                    file_refs.append(artifact.ref.as_dict())
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
                file_refs=file_refs,
                result_reference=output.result_reference,
                error_category=output.error_category,
                error_message=output.error_message,
            )
            session.add(observation)
            session.flush()
            observation_id = observation.id
        if self._observation_notifier is not None:
            self._observation_notifier(run_id, task_id)
        with self._session_factory() as session:
            persisted = session.get(SandboxObservation, observation_id)
            if persisted is None:
                raise SandboxJobError("sandbox observation disappeared after publication")
            return persisted

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
        finished = Event()
        result: list[SandboxExecutionOutput] = []
        error: list[Exception] = []

        def execute_attempt() -> None:
            try:
                result.append(executor.execute(job=job, attempt=attempt, input_refs=refs))
            except Exception as exc:
                error.append(exc)
            finally:
                finished.set()

        execution_thread = Thread(
            target=execute_attempt,
            name=f"sandbox-job-{job.id}",
            daemon=True,
        )
        execution_thread.start()
        lease_lost = False
        while not finished.wait(1):
            current_job = self.get(job.id)
            current_deadline = (
                _utc(current_job.deadline_at) if current_job is not None else None
            )
            if (
                current_job is None
                or (current_deadline is not None and current_deadline <= utc_now())
            ):
                executor.cancel(attempt)
                finished.wait()
                lease_lost = current_job is None
                break
            if not self.heartbeat(
                job_id=job.id,
                attempt_id=attempt.id,
                worker_id=worker_id,
            ):
                executor.cancel(attempt)
                finished.wait()
                current_job = self.get(job.id)
                lease_lost = (
                    current_job is None or current_job.cancel_requested_at is None
                )
                break
        if lease_lost:
            return True
        if error:
            output = SandboxExecutionOutput(
                status="failed",
                error_category="infrastructure",
                error_message=str(error[0]),
                retryable=True,
            )
        elif result:
            output = result[0]
        else:
            output = SandboxExecutionOutput(
                status="failed",
                error_category="cancelled",
                error_message="sandbox attempt was cancelled",
                retryable=False,
            )
        current_job = self.get(job.id)
        if current_job is not None and current_job.cancel_requested_at is not None:
            output = SandboxExecutionOutput(
                status="cancelled",
                stderr="sandbox execution was cancelled",
                error_category="cancelled",
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
                self._publish_unclaimed_terminal(
                    session,
                    job,
                    worker_id="cancellation",
                    status="cancelled",
                    error_category="cancelled",
                    error_message="sandbox execution was cancelled before execution",
                    now=utc_now(),
                )
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


class DockerSandboxJobExecutor:
    """Adapt durable file references to one isolated Docker Job Attempt."""

    def __init__(
        self,
        sandbox: DockerSandbox,
        file_store: ResearchFileStore,
        output_root: Path,
    ) -> None:
        self._sandbox = sandbox
        self._file_store = file_store
        self._output_root = output_root

    def execute(
        self,
        *,
        job: SandboxJob,
        attempt: SandboxAttempt,
        input_refs: tuple[ResearchFileRef | dict[str, str], ...],
    ) -> SandboxExecutionOutput:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix=f"sandbox-job-{job.id}-") as input_root:
            mounts: list[SandboxInputMount] = []
            for index, value in enumerate(input_refs):
                if isinstance(value, dict):
                    continue
                snapshot = self._file_store.read(
                    workspace_id=job.workspace_id,
                    run_id=job.run_id,
                    task_id=job.task_id,
                    ref=value,
                    shared_refs=(value,),
                )
                input_path = Path(input_root) / f"{index}-{Path(snapshot.name).name}"
                input_path.write_text(snapshot.content or "", encoding="utf-8")
                mounts.append(
                    SandboxInputMount(
                        host_path=input_path,
                        container_name=input_path.name,
                    )
                )
            result = self._sandbox.execute(
                SandboxRequest(
                    code=job.code,
                    input_mounts=mounts,
                    output_dir=(
                        self._output_root
                        / str(job.workspace_id)
                        / str(job.id)
                        / f"attempt-{attempt.attempt_number}"
                    ),
                    timeout_seconds=job.timeout_seconds,
                ),
                execution_key=f"{job.id}:{attempt.id}",
            )
            artifact_contents: list[tuple[str, str]] = []
            if result.status == "completed":
                output_root = (
                    self._output_root
                    / str(job.workspace_id)
                    / str(job.id)
                    / f"attempt-{attempt.attempt_number}"
                ).resolve()
                for artifact_path in result.artifacts:
                    resolved_artifact = artifact_path.resolve()
                    try:
                        relative_name = resolved_artifact.relative_to(output_root)
                    except ValueError as exc:
                        raise ValueError("sandbox artifact escaped the output directory") from exc
                    if not relative_name.parts:
                        raise ValueError("sandbox artifact has no relative name")
                    try:
                        content = resolved_artifact.read_text(encoding="utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError("sandbox artifacts must be UTF-8 text") from exc
                    name = str(relative_name)
                    artifact_contents.append((name, content))
        return SandboxExecutionOutput(
            status=result.status,
            stdout=result.stdout,
            stderr=result.stderr,
            artifact_contents=tuple(artifact_contents),
            result_reference=f"sandbox-job://{job.id}/attempt/{attempt.id}",
            error_category=("deadline" if result.status == "timed_out" else None),
        )

    def cancel(self, attempt: SandboxAttempt) -> bool:
        return self._sandbox.cancel(f"{attempt.job_id}:{attempt.id}")


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

    def resume(
        self,
        claim: TaskClaim,
        call: TaskToolCall,
        waiting_reference: str,
    ) -> TaskObservationResult:
        """Turn a durable Job Observation into the Task Observation."""
        from deep_researcher.task_runtime import TaskObservationResult

        del call
        prefix = "sandbox-job:"
        if not waiting_reference.startswith(prefix):
            return TaskObservationResult(
                status="failed",
                failure_ref="invalid_waiting_reference",
                error_category="protocol",
            )
        try:
            job_id = UUID(waiting_reference.removeprefix(prefix))
        except ValueError:
            return TaskObservationResult(
                status="failed",
                failure_ref="invalid_waiting_reference",
                error_category="protocol",
            )
        with self._service._session_factory() as session:
            job = session.get(SandboxJob, job_id)
            observation = session.scalar(
                select(SandboxObservation).where(SandboxObservation.job_id == job_id)
            )
            if job is None or job.run_id != claim.run_id or job.task_id != claim.task_id:
                return TaskObservationResult(
                    status="failed",
                    failure_ref="sandbox_job_not_available",
                    error_category="permission",
                )
            if observation is None:
                return TaskObservationResult(
                    status="waiting",
                    waiting_reference=waiting_reference,
                    result_reference=f"sandbox-job://{job.id}",
                    summary="Sandbox Job 仍在等待专用 Worker",
                )
            if observation.status != "completed":
                return TaskObservationResult(
                    status="failed",
                    failure_ref=observation.error_category or observation.status,
                    error_category=observation.error_category or "execution",
                    summary=observation.error_message,
                )
            return TaskObservationResult(
                status="succeeded",
                result_reference=observation.result_reference,
                file_refs=tuple(observation.file_refs),
                evidence_gain=True,
                summary=observation.stdout_preview,
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
