from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from deep_researcher.app import create_app
from deep_researcher.models import (
    ResearchPlan,
    ResearchRun,
    ResearchTask,
    RunEvent,
    TaskModelTurn,
    TaskObservation,
    TaskOutcome,
    TaskResultProposalRecord,
)
from deep_researcher.settings import Settings
from deep_researcher.task_runtime import (
    DeterministicTaskAdapter,
    DeterministicTaskModelGateway,
    DeterministicTaskToolAdapter,
    ReActTaskController,
    StaleTaskClaimError,
    TaskExecutionResult,
    TaskObservationResult,
    TaskResultProposal,
    TaskRuntime,
    TaskToolCall,
    ToolDefinition,
    ToolPolicy,
    ToolRegistry,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete, select


class ResumableWaitingAdapter:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.resume_calls = 0

    def execute(self, claim, call):
        del claim, call
        self.execute_calls += 1
        return TaskObservationResult(
            status="waiting",
            waiting_reference="approval:stable",
            failure_ref="approval_required",
        )

    def resume(self, claim, call, waiting_reference):
        del claim, call
        assert waiting_reference == "approval:stable"
        self.resume_calls += 1
        return TaskObservationResult(
            result_reference="observation:approved",
            evidence_refs=("evidence:approved",),
            evidence_gain=True,
        )


class RecoverableAdapter:
    """模拟外部副作用已完成、进程在 Observation 提交前崩溃。"""

    def __init__(self) -> None:
        self.execute_calls = 0
        self.recover_calls = 0

    def execute(self, claim, call):
        del claim, call
        self.execute_calls += 1
        return TaskObservationResult(
            result_reference="observation:external-once",
            evidence_refs=("evidence:external-once",),
            evidence_gain=True,
        )

    def recover(self, claim, call):
        del claim, call
        self.recover_calls += 1
        return TaskObservationResult(
            result_reference="observation:external-once",
            evidence_refs=("evidence:external-once",),
            evidence_gain=True,
        )


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


def test_react_controller_round_trips_observation_before_result_proposal(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'bounded-loop.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "bounded-loop"},
            json={"content": "测试 Observation 回流"},
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
            criteria = tuple(task.success_criteria)
            tool_name = task.allowed_tools[0] if task.allowed_tools else "web_search"
            session.commit()
        claim = runtime.claim(task_id, lease_owner="bounded-worker")
        assert claim is not None
        model = DeterministicTaskModelGateway(
            [
                TaskToolCall(tool_name=tool_name, arguments={"query": "bounded"}),
                TaskResultProposal(
                    result_reference="result:bounded",
                    evidence_refs=("evidence:bounded",),
                    covered_criteria=criteria,
                ),
            ]
        )
        controller = ReActTaskController(
            app.state.session_factory,
            model_gateway=model,
            tool_adapter=DeterministicTaskToolAdapter(
                TaskObservationResult(
                    result_reference="observation:bounded",
                    evidence_refs=("evidence:bounded",),
                    evidence_gain=True,
                )
            ),
        )

        first = controller.advance(claim)
        second = controller.advance(claim)
        replay = controller.advance(claim)
        with app.state.session_factory() as session:
            observations = session.scalars(
                select(TaskObservation).where(TaskObservation.task_id == task_id)
            ).all()
            turns = session.scalars(
                select(TaskModelTurn).where(TaskModelTurn.task_id == task_id)
            ).all()
            proposals = session.scalars(
                select(TaskResultProposalRecord).where(TaskResultProposalRecord.task_id == task_id)
            ).all()

    assert first.status == "runnable"
    assert second.status == "terminal"
    assert second.outcome_ref == replay.outcome_ref
    assert len(observations) == len(proposals) == 1
    assert len(turns) == 2


def test_react_controller_recovers_inflight_tool_without_second_model_turn(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'inflight-recovery.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "inflight-recovery"},
            json={"content": "测试工具副作用恢复"},
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
            tool_name = task.allowed_tools[0] if task.allowed_tools else "web_search"
            session.commit()
        claim = runtime.claim(task_id, lease_owner="recovery-worker")
        assert claim is not None
        adapter = RecoverableAdapter()
        model = DeterministicTaskModelGateway(
            [TaskToolCall(tool_name=tool_name, arguments={"query": "once"})]
        )
        controller = ReActTaskController(
            app.state.session_factory,
            model_gateway=model,
            tool_adapter=adapter,
        )

        first = controller.advance(claim)
        assert first.status == "runnable"
        with app.state.session_factory.begin() as session:
            session.execute(
                delete(TaskObservation).where(TaskObservation.task_id == task_id)
            )

        recovered = controller.advance(claim)
        with app.state.session_factory() as session:
            observation = session.scalar(
                select(TaskObservation).where(TaskObservation.task_id == task_id)
            )
            turns = session.scalars(
                select(TaskModelTurn).where(TaskModelTurn.task_id == task_id)
            ).all()

    assert recovered.status == "runnable"
    assert adapter.execute_calls == 1
    assert adapter.recover_calls == 1
    assert len(model.contexts) == 1
    assert observation is not None
    assert len(turns) == 1


def test_react_controller_rejects_model_owned_outcome_and_repairs_once(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'bounded-repair.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "bounded-repair"},
            json={"content": "测试非法模型输出"},
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
            criteria = tuple(task.success_criteria)
            session.commit()
        claim = runtime.claim(task_id, lease_owner="repair-worker")
        assert claim is not None
        controller = ReActTaskController(
            app.state.session_factory,
            model_gateway=DeterministicTaskModelGateway(
                [TaskExecutionResult(result_reference="model-cannot-complete")],
                repairs=[
                    TaskResultProposal(
                        result_reference="result:repaired",
                        evidence_refs=("evidence:repaired",),
                        covered_criteria=criteria,
                    )
                ],
            ),
        )

        result = controller.advance(claim)
        with app.state.session_factory() as session:
            turn = session.scalar(
                select(TaskModelTurn).where(TaskModelTurn.task_id == task_id)
            )
            outcome = session.scalar(select(TaskOutcome).where(TaskOutcome.task_id == task_id))

    assert result.status == "terminal"
    assert outcome is not None
    assert outcome.kind == "completed"
    assert turn is not None
    assert turn.output_kind == "result_proposal"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError(), "provider_timeout"),
        (type("RateLimitError", (RuntimeError,), {"status_code": 429})(), "provider_rate_limited"),
        (RuntimeError("provider unavailable"), "provider_error"),
    ],
)
def test_react_controller_persists_provider_failures_without_repair(
    tmp_path, error, reason
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'provider-failure.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": f"provider-failure-{reason}"},
            json={"content": "测试 provider 失败分类"},
        ).json()
        runtime = TaskRuntime(app.state.session_factory)
        with app.state.session_factory() as session:
            task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert task is not None
            claim = runtime.claim(task.id, lease_owner="provider-worker")
        assert claim is not None

        class FailingGateway:
            repair_calls = 0

            def complete_task_turn(self, _context):
                raise error

            def repair_task_turn(self, _context, _invalid):
                self.repair_calls += 1
                raise AssertionError("provider failures must not trigger controlled repair")

        gateway = FailingGateway()
        result = ReActTaskController(
            app.state.session_factory, model_gateway=gateway
        ).advance(claim)
        with app.state.session_factory() as session:
            turn = session.scalar(
                select(TaskModelTurn).where(TaskModelTurn.task_id == claim.task_id)
            )
            outcome = session.scalar(
                select(TaskOutcome).where(TaskOutcome.task_id == claim.task_id)
            )

    assert result.status == "terminal"
    assert result.reason == reason
    assert gateway.repair_calls == 0
    assert turn is not None
    assert turn.failure_reason == reason
    assert outcome is not None
    assert outcome.kind == "failed"
    assert outcome.failure_ref == reason


def test_react_controller_stops_after_two_successful_turns_without_evidence_gain(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'bounded-no-gain.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "bounded-no-gain"},
            json={"content": "测试无证据增益停止"},
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
            tool_name = task.allowed_tools[0] if task.allowed_tools else "web_search"
            session.commit()
        claim = runtime.claim(task_id, lease_owner="no-gain-worker")
        assert claim is not None
        controller = ReActTaskController(
            app.state.session_factory,
            model_gateway=DeterministicTaskModelGateway(
                [
                    TaskToolCall(tool_name=tool_name, arguments={}),
                    TaskToolCall(tool_name=tool_name, arguments={}),
                ]
            ),
            tool_adapter=DeterministicTaskToolAdapter(
                TaskObservationResult(result_reference="observation:no-gain")
            ),
        )

        first = controller.advance(claim)
        second = controller.advance(claim)

    assert first.status == "runnable"
    assert second.status == "terminal"
    assert second.reason == "no_evidence_gain"


def test_react_controller_turn_budget_and_cancellation_are_terminal_facts(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'bounded-budget.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "bounded-budget"},
            json={"content": "测试 turn budget"},
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
            tool_name = task.allowed_tools[0] if task.allowed_tools else "web_search"
            session.commit()
        claim = runtime.claim(task_id, lease_owner="budget-worker")
        assert claim is not None
        controller = ReActTaskController(
            app.state.session_factory,
            model_gateway=DeterministicTaskModelGateway(
                [
                    TaskToolCall(tool_name=tool_name, arguments={}),
                    TaskToolCall(tool_name=tool_name, arguments={}),
                ]
            ),
            tool_adapter=DeterministicTaskToolAdapter(
                TaskObservationResult(
                    result_reference="observation:budget", evidence_gain=True
                )
            ),
            max_turns=1,
        )
        first = controller.advance(claim)
        exhausted = controller.advance(claim)

        created_cancel = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "bounded-cancel"},
            json={"content": "测试取消"},
        ).json()
        cancel_task_id: UUID
        with app.state.session_factory.begin() as session:
            cancel_run = session.get(ResearchRun, UUID(created_cancel["run_id"]))
            assert cancel_run is not None
            cancel_task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == cancel_run.id)
                .order_by(ResearchTask.ordinal)
            )
            assert cancel_task is not None
            cancel_task_id = cancel_task.id
        cancel_claim = runtime.claim(cancel_task_id, lease_owner="cancel-worker")
        assert cancel_claim is not None
        with app.state.session_factory.begin() as session:
            cancel_run = session.get(ResearchRun, UUID(created_cancel["run_id"]))
            assert cancel_run is not None
            cancel_run.cancel_requested_at = datetime.now(UTC)
        cancelled = ReActTaskController(
            app.state.session_factory,
            model_gateway=DeterministicTaskModelGateway([]),
        ).advance(cancel_claim)
        with app.state.session_factory() as session:
            cancel_outcome = session.scalar(
                select(TaskOutcome).where(TaskOutcome.task_id == cancel_task_id)
            )

    assert first.status == "runnable"
    assert exhausted.status == "terminal"
    assert exhausted.reason == "turn_budget_exhausted"
    assert cancelled.status == "terminal"
    assert cancel_outcome is not None
    assert cancel_outcome.kind == "cancelled"


def test_tool_registry_rejects_unknown_and_invalid_calls_without_side_effect(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tool-policy.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "tool-policy"},
            json={"content": "测试工具策略"},
        ).json()
        runtime = TaskRuntime(app.state.session_factory)
        with app.state.session_factory.begin() as session:
            task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert task is not None
            task.allowed_tools = ["search"]
            task_id = task.id

        adapter = DeterministicTaskToolAdapter()
        registry = ToolRegistry(
            [
                ToolDefinition(
                    name="search",
                    input_schema={
                        "type": "object",
                        "required": ["query"],
                        "properties": {"query": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    result_contract={"type": "observation"},
                    handler=adapter,
                )
            ]
        )
        claim = runtime.claim(task_id, lease_owner="policy-worker")
        assert claim is not None
        unknown = ReActTaskController(
            app.state.session_factory,
            model_gateway=DeterministicTaskModelGateway(
                [TaskToolCall(tool_name="not-registered", arguments={})]
            ),
            tool_registry=registry,
        ).advance(claim)

        invalid_created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "tool-invalid"},
            json={"content": "测试非法参数"},
        ).json()
        with app.state.session_factory.begin() as session:
            task = session.get(ResearchTask, task_id)
            invalid_task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(invalid_created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert task is not None
            assert invalid_task is not None
            invalid_task.allowed_tools = ["search"]
            invalid_task_id = invalid_task.id
        invalid_claim = runtime.claim(invalid_task_id, lease_owner="policy-worker")
        assert invalid_claim is not None
        invalid = ReActTaskController(
            app.state.session_factory,
            model_gateway=DeterministicTaskModelGateway(
                [TaskToolCall(tool_name="search", arguments={"query": 42})]
            ),
            tool_registry=registry,
        ).advance(invalid_claim)

    assert unknown.reason == "unknown_tool"
    assert invalid.reason == "invalid_arguments"
    assert adapter.calls == []


def test_tool_policy_workspace_acl_rejects_before_handler_execution(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tool-acl.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "tool-acl"},
            json={"content": "测试 Workspace ACL"},
        ).json()
        runtime = TaskRuntime(app.state.session_factory)
        with app.state.session_factory.begin() as session:
            task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert task is not None
            task.allowed_tools = ["search"]
            task_id = task.id
        adapter = DeterministicTaskToolAdapter()
        claim = runtime.claim(task_id, lease_owner="acl-worker")
        assert claim is not None
        result = ReActTaskController(
            app.state.session_factory,
            model_gateway=DeterministicTaskModelGateway(
                [TaskToolCall(tool_name="search", arguments={"query": "secret"})]
            ),
            tool_registry=ToolRegistry(
                [
                    ToolDefinition(
                        name="search",
                        input_schema={"type": "object"},
                        handler=adapter,
                    )
                ]
            ),
            tool_policy=ToolPolicy(workspace_acl=lambda _workspace_id, _name: False),
        ).advance(claim)

    assert result.reason == "workspace_acl_denied"
    assert adapter.calls == []


def test_waiting_tool_resumes_by_stable_reference_and_observation_reaches_next_turn(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tool-waiting.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "tool-waiting"},
            json={"content": "测试等待恢复"},
        ).json()
        runtime = TaskRuntime(app.state.session_factory)
        with app.state.session_factory.begin() as session:
            task = session.scalar(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            )
            assert task is not None
            task.allowed_tools = ["reviewed-search"]
            task_id = task.id
            criteria = tuple(task.success_criteria)
        claim = runtime.claim(task_id, lease_owner="waiting-worker")
        assert claim is not None
        adapter = ResumableWaitingAdapter()
        model = DeterministicTaskModelGateway(
            [
                TaskToolCall(tool_name="reviewed-search", arguments={"query": "bounded"}),
                TaskResultProposal(
                    result_reference="result:waiting",
                    evidence_refs=("evidence:approved",),
                    covered_criteria=criteria,
                ),
            ]
        )
        controller = ReActTaskController(
            app.state.session_factory,
            model_gateway=model,
            tool_registry=ToolRegistry(
                [
                    ToolDefinition(
                        name="reviewed-search",
                        input_schema={"type": "object"},
                        risk="safe",
                        handler=adapter,
                    )
                ]
            ),
        )

        waiting = controller.advance(claim)
        resumed = controller.advance(claim)
        terminal = controller.advance(claim)

        with app.state.session_factory() as session:
            observations = session.scalars(
                select(TaskObservation).where(TaskObservation.task_id == task_id)
            ).all()

    assert waiting.status == "waiting"
    assert waiting.observation_ref == "task-observation:" + str(task_id) + ":1"
    assert resumed.status == "runnable"
    assert terminal.status == "terminal"
    assert adapter.execute_calls == adapter.resume_calls == 1
    assert len(observations) == 2
    assert model.contexts[1].previous_observation is not None
    assert model.contexts[1].previous_observation.result_reference == "observation:approved"


def test_failed_outcome_does_not_unlock_dependency(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime-dag-failure.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "runtime-dag-failure"},
            json={"content": "测试失败依赖不会解锁"},
        ).json()
        runtime = TaskRuntime(app.state.session_factory)
        with app.state.session_factory() as session:
            tasks = session.scalars(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(created["run_id"]))
                .order_by(ResearchTask.ordinal)
            ).all()
            first_id, second_id = tasks[0].id, tasks[1].id

        first_claim = runtime.claim(first_id, lease_owner="dag-worker")
        assert first_claim is not None
        runtime.record_outcome(
            first_claim,
            TaskExecutionResult(kind="failed", failure_ref="upstream-failed"),
        )

        assert second_id not in runtime.refresh_ready(UUID(created["run_id"]))
        assert runtime.claim(second_id, lease_owner="dag-worker") is None


def test_independent_tasks_respect_fan_out_and_form_a_barrier(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime-dag-fanout.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "runtime-dag-fanout"},
            json={"content": "测试独立任务有限并发"},
        ).json()
        run_id = UUID(created["run_id"])
        with app.state.session_factory.begin() as session:
            tasks = session.scalars(
                select(ResearchTask)
                .where(ResearchTask.run_id == run_id)
                .order_by(ResearchTask.ordinal)
            ).all()
            tasks[1].dependencies = []
            tasks[2].dependencies = [2]
            first_id, second_id = tasks[0].id, tasks[1].id
            third_id = tasks[2].id

        runtime = TaskRuntime(app.state.session_factory, max_fan_out=1)
        frontier = runtime.ready_frontier(run_id)
        assert {first_id, second_id} == set(frontier)

        first_claim = runtime.claim(first_id, lease_owner="fanout-worker")
        assert first_claim is not None
        assert runtime.claim(second_id, lease_owner="fanout-worker") is None

        runtime.record_outcome(
            first_claim,
            TaskExecutionResult(result_reference="first-independent"),
        )
        second_claim = runtime.claim(second_id, lease_owner="fanout-worker")
        assert second_claim is not None
        assert not runtime.barrier_satisfied(run_id)

        runtime.record_outcome(
            second_claim,
            TaskExecutionResult(result_reference="second-independent"),
        )
        assert not runtime.barrier_satisfied(run_id)
        third_claim = runtime.claim(third_id, lease_owner="fanout-worker")
        assert third_claim is not None
        runtime.record_outcome(
            third_claim,
            TaskExecutionResult(result_reference="third-dependent"),
        )
        assert runtime.barrier_satisfied(run_id)
