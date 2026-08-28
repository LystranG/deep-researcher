import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from deep_researcher.app import create_app
from deep_researcher.database import build_engine, build_session_factory
from deep_researcher.event_log import RunEventLog
from deep_researcher.graph import ResearchGraphRunner
from deep_researcher.models import Conversation, Message, ResearchRun, Workspace
from deep_researcher.run_queue import RunQueue
from deep_researcher.settings import Settings
from deep_researcher.worker import RunWorker
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

POSTGRES_URL = os.getenv("DEEP_RESEARCHER_POSTGRES_TEST_URL")


class CrashAfterFirstGraphUpdate:
    """在测试中模拟 Worker 于首个 checkpoint 后崩溃"""

    def __init__(self, delegate: ResearchGraphRunner) -> None:
        self._delegate = delegate
        self._crashed = False

    def run(
        self,
        run_id: UUID,
        question: str,
        *,
        research_context=None,
        model_gateway=None,
        tool_execution=None,
        map_contexts=(),
        source_map_ledger=None,
        resume=None,
        agent_role="researcher",
        skill_allowed_tools=None,
        cancellation_token=None,
        on_update=None,
    ):
        """转发 Graph 执行并只在首次更新后中断 Worker"""

        def forward(node_name, update):
            if on_update is not None:
                on_update(node_name, update)
            if not self._crashed:
                self._crashed = True
                raise KeyboardInterrupt("模拟 Worker 崩溃")

        return self._delegate.run(
            run_id,
            question,
            research_context=research_context,
            model_gateway=model_gateway,
            tool_execution=tool_execution,
            map_contexts=map_contexts,
            source_map_ledger=source_map_ledger,
            resume=resume,
            agent_role=agent_role,
            skill_allowed_tools=skill_allowed_tools,
            cancellation_token=cancellation_token,
            on_update=forward,
        )


@pytest.mark.skipif(POSTGRES_URL is None, reason="未配置 PostgreSQL 专项测试数据库")
def test_concurrent_event_writers_allocate_unique_ordered_sequences() -> None:
    assert POSTGRES_URL is not None
    engine = build_engine(POSTGRES_URL)
    session_factory = build_session_factory(engine)

    with session_factory.begin() as session:
        run_id = create_run(session)

    first_log = RunEventLog(session_factory)
    second_log = RunEventLog(session_factory)
    barrier = Barrier(2)

    def append_batch(event_log: RunEventLog, writer: str) -> list[int]:
        barrier.wait()
        return [
            event_log.append(run_id, "concurrent_progress", {"writer": writer, "index": index})
            for index in range(10)
        ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(append_batch, first_log, "first")
        second = executor.submit(append_batch, second_log, "second")
        allocated = [*first.result(), *second.result()]

    replayed = first_log.replay(run_id, after=0)

    assert sorted(allocated) == list(range(1, 21))
    assert [event.seq for event in replayed] == list(range(1, 21))
    assert {event.payload["writer"] for event in replayed} == {"first", "second"}
    engine.dispose()


def register(client: TestClient) -> dict[str, str]:
    """注册 PostgreSQL 行为测试用户并返回认证请求头"""
    response = client.post(
        "/api/v1/auth/register",
        json={"email": f"worker-{uuid4()}@example.com", "password": "correct horse battery"},
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.skipif(POSTGRES_URL is None, reason="未配置 PostgreSQL 专项测试数据库")
def test_two_postgres_workers_finish_one_run_without_duplicate_terminal_effects(tmp_path) -> None:
    """验证两个独立 PostgreSQL Worker 只产生一个终态和最终消息"""
    assert POSTGRES_URL is not None
    settings = Settings(
        database_url=POSTGRES_URL,
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        workspace = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "PostgreSQL Worker"}
        )
        conversation = client.post(
            f"/api/v1/workspaces/{workspace.json()['id']}/conversations",
            headers=headers,
            json={"title": "独立领取"},
        )
        created = client.post(
            f"/api/v1/conversations/{conversation.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": f"two-workers-{uuid4()}"},
            json={"content": "验证独立 Worker 恢复"},
        )
        run_id = created.json()["run_id"]
        workers = [
            RunWorker(RunQueue(app.state.session_factory), app.state.run_coordinator),
            RunWorker(RunQueue(app.state.session_factory), app.state.run_coordinator),
        ]
        barrier = Barrier(2)

        def claim(worker: RunWorker) -> bool:
            barrier.wait()
            return worker.run_once()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(executor.map(claim, workers))

        events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation.json()['id']}/messages", headers=headers
        ).json()["items"]

    assert sorted(claimed) == [False, True]
    assert events.text.count("event: run_completed") == 1
    assert events.text.count("event: run_failed") == 0
    assert [message["role"] for message in messages].count("assistant") == 1
    assert messages[-1]["content"]


@pytest.mark.skipif(POSTGRES_URL is None, reason="未配置 PostgreSQL 专项测试数据库")
def test_expired_lease_is_recovered_once_and_attempt_is_incremented(tmp_path) -> None:
    """验证租约过期后由新 Worker 恢复且只固化一次终态"""
    assert POSTGRES_URL is not None
    settings = Settings(
        database_url=POSTGRES_URL,
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        workspace = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "租约恢复"}
        )
        conversation = client.post(
            f"/api/v1/workspaces/{workspace.json()['id']}/conversations",
            headers=headers,
            json={"title": "过期租约"},
        )
        created = client.post(
            f"/api/v1/conversations/{conversation.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": f"lease-recovery-{uuid4()}"},
            json={"content": "验证租约恢复"},
        )
        run_id = UUID(created.json()["run_id"])
        queue = RunQueue(app.state.session_factory, lease_seconds=1)
        assert queue.claim("worker-a") == run_id
        time.sleep(0.6)
        assert queue.heartbeat(run_id, "worker-a") is True
        time.sleep(0.6)
        probing_worker = RunWorker(
            RunQueue(app.state.session_factory, lease_seconds=1),
            app.state.run_coordinator,
            owner="worker-b",
        )
        assert probing_worker.run_once() is False
        time.sleep(1.1)

        recovering_worker = RunWorker(
            queue, app.state.run_coordinator, owner="worker-b"
        )
        assert recovering_worker.run_once() is True
        events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation.json()['id']}/messages", headers=headers
        ).json()["items"]

    with app.state.session_factory() as session:
        run = session.get(ResearchRun, run_id)
        assert run is not None
        attempt = run.attempt

    assert attempt == 2
    assert events.text.count("event: run_completed") == 1
    assert messages[-1]["content"]


@pytest.mark.skipif(POSTGRES_URL is None, reason="未配置 PostgreSQL 专项测试数据库")
def test_postgres_cancelled_queue_item_is_not_claimed_or_completed(tmp_path) -> None:
    """验证取消优先于队列领取且不会生成最终回答"""
    assert POSTGRES_URL is not None
    settings = Settings(
        database_url=POSTGRES_URL,
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        workspace = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "取消优先"}
        )
        conversation = client.post(
            f"/api/v1/workspaces/{workspace.json()['id']}/conversations",
            headers=headers,
            json={"title": "取消队列"},
        )
        created = client.post(
            f"/api/v1/conversations/{conversation.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": f"cancel-priority-{uuid4()}"},
            json={"content": "取消后不应执行"},
        )
        run_id = created.json()["run_id"]
        cancelled = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
        assert cancelled.json()["status"] == "cancelled"
        assert app.state.run_worker.run_once() is False
        events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation.json()['id']}/messages", headers=headers
        ).json()["items"]

    assert events.text.count("event: run_cancelled") == 1
    assert "event: run_completed" not in events.text
    assert messages[-1]["content"] == ""


@pytest.mark.skipif(POSTGRES_URL is None, reason="未配置 PostgreSQL 专项测试数据库")
def test_worker_restart_resumes_graph_checkpoint_without_duplicate_answer(tmp_path) -> None:
    """验证 Worker 崩溃后 Graph checkpoint 可恢复且答案只固化一次"""
    assert POSTGRES_URL is not None
    settings = Settings(
        database_url=POSTGRES_URL,
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        graph_runner=CrashAfterFirstGraphUpdate(ResearchGraphRunner(POSTGRES_URL)),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        workspace = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "checkpoint 恢复"}
        )
        conversation = client.post(
            f"/api/v1/workspaces/{workspace.json()['id']}/conversations",
            headers=headers,
            json={"title": "Worker 重启"},
        )
        created = client.post(
            f"/api/v1/conversations/{conversation.json()['id']}/messages",
            headers={**headers, "Idempotency-Key": f"checkpoint-restart-{uuid4()}"},
            json={"content": "验证 checkpoint 恢复"},
        )
        run_id = UUID(created.json()["run_id"])
        crashing_worker = RunWorker(
            RunQueue(app.state.session_factory, lease_seconds=1),
            app.state.run_coordinator,
            owner="crashing-worker",
        )
        with pytest.raises(KeyboardInterrupt):
            crashing_worker.run_once()
        time.sleep(1.1)
        recovering_worker = RunWorker(
            RunQueue(app.state.session_factory, lease_seconds=1),
            app.state.run_coordinator,
            owner="recovering-worker",
        )
        assert recovering_worker.run_once() is True
        events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation.json()['id']}/messages", headers=headers
        ).json()["items"]

    assert events.text.count("event: run_completed") == 1
    assert events.text.count("event: research_progress") >= 1
    assert [message["role"] for message in messages].count("assistant") == 1
    assert messages[-1]["content"]


def create_run(session: Session) -> UUID:
    workspace = Workspace(name="PostgreSQL 并发验收")
    session.add(workspace)
    session.flush()
    conversation = Conversation(workspace_id=workspace.id, title="事件序号")
    session.add(conversation)
    session.flush()
    user_message = Message(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        role="user",
        content="并发测试",
    )
    assistant_message = Message(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        role="assistant",
        content="",
    )
    session.add_all([user_message, assistant_message])
    session.flush()
    run = ResearchRun(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        trigger_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        idempotency_key=f"postgres-concurrency-{workspace.id}",
        status="running",
        next_event_seq=1,
    )
    session.add(run)
    session.flush()
    return run.id
