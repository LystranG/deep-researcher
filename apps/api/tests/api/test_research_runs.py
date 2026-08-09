import asyncio
import json
from threading import Event

from deep_researcher.app import create_app
from deep_researcher.graph import ResearchGraphRunner
from deep_researcher.model_gateway import BudgetExceededError
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from deep_researcher.web_search import DisabledWebSearchGateway
from fastapi.testclient import TestClient


class BudgetExhaustedGateway:
    """在测试中模拟模型预算耗尽"""

    def stream_answer(self, context):
        raise BudgetExceededError("模型预算已耗尽")


class UsageReportingGateway:
    """模拟返回可审计 token 和费用的模型 Adapter"""

    def stream_answer(self, context):
        yield "已完成"

    def last_usage(self):
        """返回不含 prompt 和凭证的模型计量数据"""
        return {"input_tokens": 12, "output_tokens": 3, "cost_usd": 0.0042}


class BudgetProbeGateway:
    """预算充足时返回可识别结论"""

    async def astream_answer(self, context):
        yield "预算测试模型结论"


class CancellableStreamingGateway:
    """持续生成直到运行时取消 token 生效"""

    def __init__(self) -> None:
        self.started = Event()

    async def astream_answer(self, context):
        self.started.set()
        yield "取消前草稿"
        while True:
            await asyncio.sleep(0.01)
            if context.cancellation_token is not None:
                context.cancellation_token.raise_if_cancelled()


class OneBranchFailureResearcher:
    """让一个 Researcher 分支失败而另一个正常完成"""

    async def research(self, brief, sources):
        if brief["ordinal"] == 1:
            raise RuntimeError("分支不可用")
        return {
            "ordinal": brief["ordinal"],
            "status": "completed",
            "summary": "另一个分支已完成",
            "source_ids": [source["source_chunk_id"] for source in sources],
            "failure_impact": None,
        }


def register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "researcher@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_conversation(client: TestClient, headers: dict[str, str]) -> str:
    workspace_id = client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "研究空间"}
    ).json()["id"]
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "研究会话"},
    ).json()["id"]


def test_retrying_same_message_is_idempotent(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        request_headers = {**headers, "Idempotency-Key": "message-001"}

        first = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=request_headers,
            json={"content": "分析第二季度市场变化"},
        )
        retried = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=request_headers,
            json={"content": "分析第二季度市场变化"},
        )
        client.get(f"/api/v1/runs/{first.json()['run_id']}/events", headers=headers)
        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)

    assert first.status_code == 202
    assert retried.status_code == 202
    assert first.json()["run_id"] == retried.json()["run_id"]
    assert [message["content"] for message in messages.json()["items"]] == [
        "分析第二季度市场变化",
        "已完成对“分析第二季度市场变化”的初步研究。",
    ]


def test_model_usage_is_visible_without_exposing_prompt_or_secret(tmp_path) -> None:
    """验证模型计量通过安全业务事件公开"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(settings, model_gateway=UsageReportingGateway()) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "usage-visible"},
            json={"content": "统计本次模型用量"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        ).json()

    assert "event: model_usage_recorded" in events.text
    assert "input_tokens" in events.text
    assert "cost_usd" in events.text
    assert "correct horse battery" not in events.text
    assert detail["usage"] == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cost_usd": 0.0042,
    }


def test_workspace_quota_failure_stops_before_model_conclusion(tmp_path) -> None:
    """验证 Workspace 配额不足时模型调用前失败且下游任务跳过"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
        workspace_token_quota=0,
    )
    with running_worker_client(settings, model_gateway=BudgetProbeGateway()) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "quota-preflight"},
            json={"content": "预算不足时不要调用模型"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        ).json()

    assert detail["status"] == "failed"
    assert detail["usage"] is None
    assert [task["status"] for task in detail["tasks"]] == ["failed", "skipped", "skipped"]
    assert all(task["failure_impact"] for task in detail["tasks"])
    assert "event: run_failed" in events.text
    assert "event: assistant_delta" not in events.text
    assert "预算测试模型结论" not in events.text


def test_complex_research_budget_failure_skips_downstream_work_and_exposes_impact(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings, model_gateway=BudgetExhaustedGateway()) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "bounded-dag"},
            json={"content": "比较两种方案，分析证据差异并给出可核验结论"},
        ).json()
        events = client.get(f"/api/v1/runs/{created['run_id']}/events", headers=headers)
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        ).json()

    assert events.status_code == 200
    assert "event: run_failed" in events.text
    assert detail["status"] == "failed"
    assert detail["tasks"][0]["status"] == "failed"
    assert detail["tasks"][1]["status"] == "skipped"
    assert detail["tasks"][2]["status"] == "skipped"
    assert all(task["failure_impact"] for task in detail["tasks"])


def test_one_researcher_branch_failure_keeps_other_work_and_marks_evidence_insufficient(
    tmp_path,
) -> None:
    """验证单分支失败后仍完成回答并明确证据不足"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    graph_runner = ResearchGraphRunner(researcher_gateway=OneBranchFailureResearcher())
    with running_worker_client(
        settings,
        graph_runner=graph_runner,
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "partial-researcher"},
            json={"content": "一个分支失败时继续研究"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        ).json()
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

    assert detail["status"] == "completed"
    assert [task["status"] for task in detail["tasks"]] == ["failed", "completed", "completed"]
    assert detail["tasks"][0]["failure_impact"] == "部分研究分支失败，最终回答的证据可能不完整"
    assert "证据不足" in messages[-1]["content"]
    assert "event: run_completed" in events.text
    assert "event: run_failed" not in events.text
    assert "[" not in messages[-1]["content"]


def test_reconnecting_stream_replays_only_events_after_last_event_id(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings, web_search_gateway=DisabledWebSearchGateway()
    ) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "message-stream-001"},
            json={"content": "总结当前问题"},
        ).json()

        resumed = client.get(
            f"/api/v1/runs/{created['run_id']}/events",
            headers={**headers, "Last-Event-ID": "2"},
        )
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

    assert resumed.status_code == 200
    assert "id: 1\n" not in resumed.text
    assert "id: 2\n" not in resumed.text
    assert "event: assistant_delta" in resumed.text
    assert "event: run_completed" in resumed.text
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "已完成对“总结当前问题”的初步研究。"


def test_stopping_run_prevents_new_answer_content(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
        research_step_delay_seconds=0.2,
    )

    with running_worker_client(settings) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "message-cancel-001"},
            json={"content": "执行一个较长研究"},
        ).json()

        stopped = client.post(f"/api/v1/runs/{created['run_id']}/cancel", headers=headers)
        events = client.get(f"/api/v1/runs/{created['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

    assert stopped.status_code == 200
    assert "event: run_cancelled" in events.text
    assert "event: assistant_delta" not in events.text
    assert messages[-1]["content"] == ""


def test_cancelling_run_does_not_publish_usage_or_answer_after_graph_started(tmp_path) -> None:
    """验证取消传播会阻止取消后的模型用量和回答事件"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    settings.research_step_delay_seconds = 0.02
    with running_worker_client(settings) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "cancel-no-usage"},
            json={"content": "取消后不应继续生成结论"},
        ).json()
        cancelled = client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=headers)
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)

    assert cancelled.json()["status"] == "cancelled"
    assert "event: run_cancelled" in events.text
    assert "event: assistant_delta" not in events.text
    assert "event: model_usage_recorded" not in events.text
    assert "event: run_completed" not in events.text


def test_cancelling_active_model_stream_maps_to_cancelled_without_new_conclusion(tmp_path) -> None:
    """验证模型流中的取消 token 终止运行且不固化草稿"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    gateway = CancellableStreamingGateway()
    with running_worker_client(settings, model_gateway=gateway) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "cancel-model-stream"},
            json={"content": "生成过程中停止"},
        ).json()
        assert gateway.started.wait(timeout=2)
        cancelled = client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=headers)
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

    assert cancelled.json()["status"] in {"cancel_requested", "cancelled"}
    assert "event: run_cancelled" in events.text
    assert "event: run_failed" not in events.text
    assert "event: assistant_delta" not in events.text
    assert "event: run_completed" not in events.text
    assert messages[-1]["content"] == ""


def test_conversation_can_discover_active_run_after_page_reload(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
        research_step_delay_seconds=0.2,
    )

    with running_worker_client(settings) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "reload-active-run"},
            json={"content": "刷新后继续展示"},
        ).json()

        discovered = client.get(
            f"/api/v1/conversations/{conversation_id}/active-run", headers=headers
        )
        client.get(f"/api/v1/runs/{created['run_id']}/events", headers=headers)
        after_completion = client.get(
            f"/api/v1/conversations/{conversation_id}/active-run", headers=headers
        )

    assert discovered.status_code == 200
    assert discovered.json()["run_id"] == created["run_id"]
    assert after_completion.json() is None


def test_api_only_enqueues_until_an_independent_worker_claims_the_run(tmp_path) -> None:
    """验证 API 入队与独立 Worker 执行之间的业务边界"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "independent-worker-001"},
            json={"content": "只在 Worker 领取后执行"},
        )
        queued_run = client.get(
            f"/api/v1/conversations/{conversation_id}/active-run", headers=headers
        )
        queued_messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        )

        assert created.status_code == 202
        assert created.json()["status"] == "queued"
        assert queued_run.json()["status"] == "queued"
        assert queued_messages.json()["items"][-1]["content"] == ""

        assert app.state.run_worker.run_once() is True
        completed_events = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/events", headers=headers
        )
        completed_messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        )

    assert "event: run_queued" in completed_events.text
    assert "event: run_started" in completed_events.text
    assert "event: run_completed" in completed_events.text
    assert completed_messages.json()["items"][-1]["content"]


def test_complex_research_reports_four_stable_progress_stages(tmp_path) -> None:
    """验证复杂研究通过 SSE 依次报告四个稳定阶段"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "graph-projector-001"},
            json={"content": "比较两种方案，研究证据差异并给出可核验结论"},
        )
        assert app.state.run_worker.run_once() is True
        events = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/events", headers=headers
        )

    encoded_payloads = [
        json.loads(line.removeprefix("data: "))
        for line in events.text.splitlines()
        if line.startswith("data: ")
    ]
    payloads = [
        json.loads(payload) if isinstance(payload, str) else payload
        for payload in encoded_payloads
    ]
    stages = [payload["stage"] for payload in payloads if "stage" in payload]
    assert stages == ["planning", "researching", "verifying", "writing"]
    assert events.text.index("event: run_started") < events.text.index("planning")
    assert "event: planner" not in events.text


def test_calculation_research_exposes_agent_sandbox_todo_and_sse_event(tmp_path) -> None:
    """验证 Agent 按研究需要创建可见的 Python Sandbox Todo"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(settings) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "sandbox-todo"},
            json={"content": "计算 12 * 7 并说明计算过程"},
        ).json()
        events = client.get(f"/api/v1/runs/{created['run_id']}/events", headers=headers)
        todos = client.get(f"/api/v1/runs/{created['run_id']}/todos", headers=headers)

    assert todos.status_code == 200
    assert "event: todo_created" in events.text
    sandbox_todos = [item for item in todos.json()["items"] if item["kind"] == "python_sandbox"]
    assert len(sandbox_todos) == 1
    assert sandbox_todos[0]["status"] in {"running", "completed", "failed", "skipped"}


def test_first_question_renames_default_conversation(tmp_path) -> None:
    """验证首条研究问题会替换默认会话标题"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(settings) as client:
        headers = register(client)
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "研究空间"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "新会话"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "auto-title"},
            json={"content": "比较两种客户留存策略的优劣"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        conversations = client.get(
            f"/api/v1/workspaces/{workspace_id}/conversations", headers=headers
        ).json()["items"]

    assert conversations[0]["title"] == "比较两种客户留存策略的优劣"
