from uuid import UUID

import pytest
from deep_researcher.app import create_app
from deep_researcher.models import ResearchTask, SandboxObservation
from deep_researcher.sandbox_jobs import (
    SandboxExecutionOutput,
    SandboxJobError,
    SandboxJobService,
)
from deep_researcher.settings import Settings
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
