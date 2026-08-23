from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from deep_researcher.app import create_app
from deep_researcher.models import ResearchPlan, ResearchTask, RunEvent, TaskOutcome
from deep_researcher.settings import Settings
from deep_researcher.task_runtime import (
    DeterministicTaskAdapter,
    StaleTaskClaimError,
    TaskExecutionResult,
    TaskRuntime,
)
from fastapi.testclient import TestClient
from sqlalchemy import select


def register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "runtime@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_conversation(client: TestClient, headers: dict[str, str]) -> str:
    workspace_id = client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Runtime v2"}
    ).json()["id"]
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "研究会话"},
    ).json()["id"]


def test_plan_v1_and_task_contract_are_persisted_before_worker_claims(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "runtime-plan-v1"},
            json={"content": "验证一个可恢复的研究计划"},
        ).json()

        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run",
            headers=headers,
        ).json()
        with app.state.session_factory() as session:
            plan_event = session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == UUID(created["run_id"]),
                    RunEvent.event_key == "plan-created:v1",
                )
            )

    assert detail["status"] == "queued"
    assert detail["plan"]["version"] == 1
    assert detail["plan"]["plan_hash"]
    assert len(detail["plan"]["tasks"]) >= 1
    assert detail["tasks"][0]["state"] == "ready"
    assert detail["tasks"][0]["goal"]
    assert detail["tasks"][0]["success_criteria"]
    assert detail["tasks"][0]["local_budget"]["token_budget"] > 0
    assert detail["tasks"][1]["dependencies"] == [1]
    assert detail["tasks"][1]["state"] == "pending"
    assert plan_event is not None
    assert plan_event.type == "plan_created"


def test_fencing_rejects_old_claim_and_outcome_replay_is_immutable(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime-fencing.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "runtime-fencing"},
            json={"content": "测试任务接管"},
        ).json()
        session_factory = app.state.session_factory
        task_runtime = TaskRuntime(session_factory)
        with session_factory() as session:
            task = session.scalar(
                select(ResearchTask)
                    .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert task is not None
            task_id = task.id
            session.commit()

        first_claim = task_runtime.claim(task_id, lease_owner="worker-a", lease_seconds=30)
        assert first_claim is not None
        with session_factory.begin() as session:
            task = session.get(ResearchTask, task_id)
            assert task is not None
            task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        second_claim = task_runtime.claim(task_id, lease_owner="worker-b", lease_seconds=30)
        assert second_claim is not None
        assert second_claim.fencing_epoch == first_claim.fencing_epoch + 1

        with pytest.raises(StaleTaskClaimError):
            task_runtime.record_outcome(
                first_claim,
                TaskExecutionResult(result_reference="stale-result"),
            )

        outcome = task_runtime.record_outcome(
            second_claim,
            TaskExecutionResult(result_reference="durable-result"),
        )
        replay = task_runtime.record_outcome(
            second_claim,
            TaskExecutionResult(result_reference="different-replay"),
        )
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run",
            headers=headers,
        ).json()
        with session_factory() as session:
            outcomes = session.scalars(
                select(TaskOutcome).where(TaskOutcome.task_id == task_id)
            ).all()

    assert outcome.outcome_ref == replay.outcome_ref
    assert outcome.result_reference == replay.result_reference == "durable-result"
    assert len(outcomes) == 1
    assert detail["tasks"][0]["state"] == "terminal"
    assert detail["tasks"][0]["outcome_ref"] == outcome.outcome_ref


def test_expired_claim_cannot_publish_until_task_is_reclaimed(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime-expiry.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "runtime-expiry"},
            json={"content": "测试过期租约"},
        ).json()
        runtime = TaskRuntime(app.state.session_factory)
        with app.state.session_factory() as session:
            task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert task is not None
            task_id = task.id
            session.commit()

        claim = runtime.claim(task_id, lease_owner="worker", lease_seconds=1)
        assert claim is not None
        with app.state.session_factory.begin() as session:
            task = session.get(ResearchTask, task_id)
            assert task is not None
            task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        with pytest.raises(StaleTaskClaimError):
            runtime.record_outcome(
                claim,
                TaskExecutionResult(result_reference="late-result"),
            )


def test_plan_and_outcome_rows_reject_mutation(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime-immutable.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "runtime-immutable"},
            json={"content": "测试不可变事实"},
        ).json()
        runtime = TaskRuntime(app.state.session_factory)
        with app.state.session_factory() as session:
            plan_row = session.scalar(
                select(ResearchPlan).where(
                    ResearchPlan.run_id == UUID(created["run_id"])
                )
            )
            task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert plan_row is not None
            assert task is not None
            task_id = task.id
            session.commit()
        claim = runtime.claim(task_id, lease_owner="worker")
        assert claim is not None
        outcome = runtime.record_outcome(claim, TaskExecutionResult())

        with pytest.raises(ValueError, match="immutable"):
            with app.state.session_factory.begin() as session:
                plan_row = session.get(ResearchPlan, plan_row.id)
                assert plan_row is not None
                plan_row.goal = "mutated"
        with pytest.raises(ValueError, match="immutable"):
            with app.state.session_factory.begin() as session:
                persisted = session.get(TaskOutcome, outcome.id)
                assert persisted is not None
                persisted.kind = "failed"


def test_advance_uses_deterministic_adapter_and_unlocks_dependency(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime-advance.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "runtime-advance"},
            json={"content": "验证依赖完成后解锁任务"},
        ).json()
        session_factory = app.state.session_factory
        task_runtime = TaskRuntime(session_factory)
        with session_factory() as session:
            tasks = session.scalars(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            ).all()
            first_id = tasks[0].id
            second_id = tasks[1].id

        assert task_runtime.claim(second_id, lease_owner="worker") is None
        first_outcome = task_runtime.advance(
            first_id,
            lease_owner="worker",
            adapter=DeterministicTaskAdapter(
                TaskExecutionResult(result_reference="first-result")
            ),
        )
        assert first_outcome is not None
        ready = task_runtime.refresh_ready(UUID(created["run_id"]))
        assert second_id in ready
        second_outcome = task_runtime.advance(
            second_id,
            lease_owner="worker",
            adapter=DeterministicTaskAdapter(
                TaskExecutionResult(result_reference="second-result")
            ),
        )

    assert second_outcome is not None
    assert second_outcome.kind == "completed"
