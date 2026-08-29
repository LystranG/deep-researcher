from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from deep_researcher.app import create_app
from deep_researcher.models import ResearchTask, SandboxJob, SandboxObservation
from deep_researcher.sandbox import SandboxResult
from deep_researcher.sandbox_jobs import (
    DockerSandboxJobExecutor,
    SandboxExecutionOutput,
    SandboxJobError,
    SandboxJobService,
    SandboxJobWorker,
    SandboxToolAdapter,
)
from deep_researcher.settings import Settings
from deep_researcher.task_runtime import TaskRuntime, TaskToolCall
from deep_researcher.worker import RunWorker
from fastapi.testclient import TestClient
from sqlalchemy import select


def _job_context(client: TestClient, app) -> tuple[SandboxJobService, UUID, UUID, UUID]:
    auth = client.post(
        "/api/v1/auth/register",
        json={"email": "sandbox-jobs@example.com", "password": "correct horse battery"},
    ).json()
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    workspace = client.post("/api/v1/workspaces", headers=headers, json={"name": "Jobs"}).json()
    conversation = client.post(
        f"/api/v1/workspaces/{workspace['id']}/conversations",
        headers=headers,
        json={"title": "Sandbox jobs"},
    ).json()
    run = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers={**headers, "Idempotency-Key": "sandbox-job-test"},
        json={"content": "运行数据分析"},
    ).json()
    with app.state.session_factory() as session:
        task = session.scalar(
            select(ResearchTask)
            .where(ResearchTask.run_id == UUID(run["run_id"]))
            .order_by(ResearchTask.ordinal)
        )
        assert task is not None
        task_id = task.id
    return (
        SandboxJobService(app.state.session_factory, app.state.research_file_store),
        UUID(workspace["id"]),
        UUID(run["run_id"]),
        task_id,
    )


def test_run_worker_executes_durable_sandbox_job_through_docker_adapter(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            object_store_root=tmp_path / "objects",
            sandbox_output_root=tmp_path / "sandbox-output",
        ),
        embedded_worker=False,
    )

    class FakeSandbox:
        def execute(self, request, *, execution_key=None):
            assert request.code == "print(42)"
            assert execution_key is not None
            return SandboxResult(status="completed", stdout="42", stderr="", artifacts=[])

        def cancel(self, execution_key):
            return True

    with TestClient(app) as client:
        service, workspace_id, run_id, task_id = _job_context(client, app)
        job = service.submit(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            invocation_key="worker-durable-sandbox",
            purpose="inspect_data",
            code="print(42)",
        )
        sandbox_worker = SandboxJobWorker(
            service,
            DockerSandboxJobExecutor(
                FakeSandbox(), app.state.research_file_store, tmp_path / "sandbox-output"
            ),
            worker_id="sandbox-worker",
        )
        queue = app.state.run_queue
        queue.claim = lambda _owner: None
        worker = RunWorker(queue, app.state.runtime, sandbox_runtime=sandbox_worker)
        assert worker.run_once() is True
        with app.state.session_factory() as session:
            observation = session.scalar(
                select(SandboxObservation).where(SandboxObservation.job_id == job.id)
            )
        assert observation is not None
        assert observation.status == "completed"
    assert observation.stdout_preview == "42"


def test_docker_executor_persists_text_artifacts_in_file_space(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'artifacts.db'}",
            object_store_root=tmp_path / "objects",
            sandbox_output_root=tmp_path / "sandbox-output",
        ),
        embedded_worker=False,
    )

    class FakeSandbox:
        def execute(self, request, *, execution_key=None):
            assert execution_key is not None
            request.output_dir.mkdir(parents=True, exist_ok=True)
            (request.output_dir / "result.txt").write_text("artifact output", encoding="utf-8")
            return SandboxResult(
                status="completed",
                stdout="done",
                stderr="",
                artifacts=[request.output_dir / "result.txt"],
            )

        def cancel(self, execution_key):
            return True

    with TestClient(app) as client:
        service, workspace_id, run_id, task_id = _job_context(client, app)
        job = service.submit(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            invocation_key="artifact-round-trip",
            purpose="transform_artifact",
            code="print(42)",
        )
        claimed = service.claim(worker_id="artifact-worker")
        assert claimed is not None
        _, attempt = claimed
        executor = DockerSandboxJobExecutor(
            FakeSandbox(), app.state.research_file_store, tmp_path / "sandbox-output"
        )
        output = executor.execute(
            job=job,
            attempt=attempt,
            input_refs=(),
        )
        observation = service.publish(
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id="artifact-worker",
            output=output,
        )
        assert len(observation.file_refs) == 1
        artifact_ref = observation.file_refs[0]
        snapshot = app.state.research_file_store.read(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            ref=artifact_ref,
        )

    assert snapshot.ref.kind == "artifact"
    assert snapshot.content == "artifact output"


def test_task_tool_resumes_from_durable_sandbox_observation(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            object_store_root=tmp_path / "objects",
        ),
        embedded_worker=False,
    )
    with TestClient(app) as client:
        service, _workspace_id, _run_id, task_id = _job_context(client, app)
        claim = TaskRuntime(app.state.session_factory).claim(
            task_id,
            lease_owner="runtime-worker",
        )
        assert claim is not None
        adapter = SandboxToolAdapter(service)
        call = TaskToolCall(
            tool_name="python_execute",
            arguments={"purpose": "inspect_data", "code": "print(42)"},
            logical_call_ref="sandbox-resume",
        )
        waiting = adapter.execute(claim, call)
        assert waiting.waiting_reference is not None

        class Executor:
            def execute(self, **_kwargs):
                return SandboxExecutionOutput(
                    status="completed",
                    stdout="42",
                    result_reference="sandbox-result:42",
                )

            def cancel(self, _attempt):
                return True

        assert service.run_once(worker_id="sandbox-worker", executor=Executor()) is True
        resumed = adapter.resume(claim, call, waiting.waiting_reference)

    assert resumed.status == "succeeded"
    assert resumed.evidence_gain is True
    assert resumed.result_reference == "sandbox-result:42"
    assert resumed.summary == "42"


def test_expired_unclaimed_job_persists_terminal_observation(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            object_store_root=tmp_path / "objects",
        ),
        embedded_worker=False,
    )
    with TestClient(app) as client:
        service, workspace_id, run_id, task_id = _job_context(client, app)
        job = service.submit(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            invocation_key="expired-before-claim",
            purpose="inspect_data",
            code="print(42)",
            timeout_seconds=1,
        )
        with app.state.session_factory.begin() as session:
            persisted = session.get(SandboxJob, job.id)
            assert persisted is not None
            persisted.deadline_at = datetime.now(UTC) - timedelta(seconds=1)

        assert service.claim(worker_id="sandbox-worker") is None
        with app.state.session_factory() as session:
            persisted = session.get(SandboxJob, job.id)
            observation = session.scalar(
                select(SandboxObservation).where(SandboxObservation.job_id == job.id)
            )

    assert persisted is not None
    assert persisted.status == "timed_out"
    assert observation is not None
    assert observation.status == "timed_out"
    assert observation.error_category == "deadline"


def test_cancelled_unclaimed_job_resumes_as_terminal_failure(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            object_store_root=tmp_path / "objects",
        ),
        embedded_worker=False,
    )
    with TestClient(app) as client:
        service, _workspace_id, _run_id, task_id = _job_context(client, app)
        claim = TaskRuntime(app.state.session_factory).claim(
            task_id,
            lease_owner="runtime-worker",
        )
        assert claim is not None
        adapter = SandboxToolAdapter(service)
        call = TaskToolCall(
            tool_name="python_execute",
            arguments={"purpose": "inspect_data", "code": "print(42)"},
            logical_call_ref="cancel-before-claim",
        )
        waiting = adapter.execute(claim, call)
        assert waiting.waiting_reference is not None
        job_id = UUID(waiting.waiting_reference.removeprefix("sandbox-job:"))

        service.cancel(job_id)
        resumed = adapter.resume(claim, call, waiting.waiting_reference)

    assert resumed.status == "failed"
    assert resumed.failure_ref == "cancelled"


def test_unknown_purpose_is_structurally_rejected(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            object_store_root=tmp_path / "objects",
        ),
        embedded_worker=False,
    )
    with TestClient(app) as client:
        service, workspace_id, run_id, task_id = _job_context(client, app)
        with pytest.raises(SandboxJobError, match="unsupported"):
            service.submit(
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                invocation_key="bad-purpose",
                purpose="run_shell",
                code="print(1)",
            )


def test_job_claim_and_publish_are_idempotent_and_cancel_wins(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            object_store_root=tmp_path / "objects",
        ),
        embedded_worker=False,
    )
    with TestClient(app) as client:
        service, workspace_id, run_id, task_id = _job_context(client, app)
        job = service.submit(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            invocation_key="cancel-race",
            purpose="inspect_data",
            code="print(1)",
        )
        replay = service.submit(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            invocation_key="cancel-race",
            purpose="inspect_data",
            code="print(1)",
        )
        assert replay.id == job.id
        claimed = service.claim(worker_id="sandbox-worker-a")
        assert claimed is not None
        claimed_job, attempt = claimed
        service.cancel(job.id)
        observation = service.publish(
            job_id=claimed_job.id,
            attempt_id=attempt.id,
            worker_id="sandbox-worker-a",
            output=SandboxExecutionOutput(status="completed", stdout="late success"),
        )
        replayed_observation = service.publish(
            job_id=claimed_job.id,
            attempt_id=attempt.id,
            worker_id="sandbox-worker-a",
            output=SandboxExecutionOutput(status="completed", stdout="different"),
        )

        assert observation.status == "cancelled"
        assert replayed_observation.id == observation.id
        with app.state.session_factory() as session:
            assert session.scalar(
                select(SandboxObservation).where(SandboxObservation.job_id == job.id)
            ) is not None


def test_infrastructure_failure_is_retried_but_publishes_one_observation(tmp_path) -> None:
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            object_store_root=tmp_path / "objects",
        ),
        embedded_worker=False,
    )

    class FlakyExecutor:
        calls = 0

        def execute(self, *, job, attempt, input_refs):
            self.calls += 1
            if self.calls == 1:
                return SandboxExecutionOutput(
                    status="failed",
                    error_category="infrastructure",
                    error_message="worker disconnected",
                    retryable=True,
                )
            return SandboxExecutionOutput(status="completed", stdout="ok")

        def cancel(self, attempt):
            return True

    with TestClient(app) as client:
        service, workspace_id, run_id, task_id = _job_context(client, app)
        job = service.submit(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            invocation_key="retryable-job",
            purpose="transform_artifact",
            code="print('ok')",
        )
        executor = FlakyExecutor()
        assert service.run_once(worker_id="sandbox-worker-a", executor=executor)
        assert service.run_once(worker_id="sandbox-worker-a", executor=executor)
        with app.state.session_factory() as session:
            observations = session.scalars(
                select(SandboxObservation).where(SandboxObservation.job_id == job.id)
            ).all()
        assert len(observations) == 1
        assert observations[0].status == "completed"
        assert observations[0].attempt_count == 2
