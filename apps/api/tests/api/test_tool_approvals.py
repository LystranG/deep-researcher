import socket
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from threading import Thread

import pytest
from deep_researcher.app import create_app
from deep_researcher.settings import Settings
from deep_researcher.tool_execution import (
    McpProtocolError,
    McpResultValidationError,
    McpToolError,
    McpTransportError,
)
from fastapi.testclient import TestClient
from mcp.server import MCPServer
from uvicorn import Config, Server


class RejectIfCalledMcpGateway:
    """提供受信工具发现，并在拒绝后发生真实调用时让业务测试失败"""

    async def list_tools(self) -> list[dict[str, object]]:
        """返回本地受信 MCP 示例工具"""
        return [
            {
                "name": "research_notes.create",
                "description": "创建一条研究记录",
                "input_schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
            }
        ]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """拒绝路径不允许进入真实副作用 Adapter"""
        raise RuntimeError(f"拒绝后不得调用 {name}: {arguments}")


class MultiToolMcpGateway(RejectIfCalledMcpGateway):
    """提供一个超出 Agent 白名单的额外工具用于目录过滤验收"""

    async def list_tools(self) -> list[dict[str, object]]:
        """返回包含允许与未允许工具的受信目录"""
        return [
            *await super().list_tools(),
            {
                "name": "research_notes.delete",
                "description": "删除研究记录",
                "input_schema": {"type": "object"},
            },
        ]


class SingleUseMcpGateway:
    """提供一次成功副作用，重复执行时让公共运行失败"""

    def __init__(self) -> None:
        """初始化尚未执行的本地工具状态"""
        self._executed = False

    async def list_tools(self) -> list[dict[str, object]]:
        """返回本地受信 MCP 示例工具"""
        return [
            {
                "name": "research_notes.create",
                "description": "创建一条研究记录",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": ["title", "idempotency_key"],
                },
            }
        ]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """首次调用返回安全结果，重复调用直接失败"""
        if self._executed:
            raise RuntimeError("同一个工具调用不得重复产生副作用")
        self._executed = True
        return {"summary": f"已创建研究记录：{arguments['title']}"}


class FailingMcpGateway(SingleUseMcpGateway):
    """模拟批准后下游调用失败且包含不可公开详情"""

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """抛出包含敏感下游详情的调用异常"""
        raise RuntimeError("downstream-secret-detail")


class CategorizedFailingMcpGateway(SingleUseMcpGateway):
    """模拟具有固定公开分类的 MCP 下游失败"""

    def __init__(self, error: Exception) -> None:
        """保存批准后需要抛出的分类异常"""
        super().__init__()
        self._error = error

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """抛出指定 MCP 分类异常"""
        raise self._error


def register(client: TestClient) -> dict[str, str]:
    """注册测试用户并返回认证头"""
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "tool-approval@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_conversation(client: TestClient, headers: dict[str, str]) -> str:
    """创建工具审批测试使用的空间和会话"""
    _, conversation_id = create_workspace_and_conversation(client, headers)
    return conversation_id


def create_workspace_and_conversation(
    client: TestClient, headers: dict[str, str]
) -> tuple[str, str]:
    """创建工具审批测试使用的空间和会话并返回两个标识"""
    workspace_id = client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "工具审批空间"}
    ).json()["id"]
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "工具审批会话"},
    ).json()["id"]
    return workspace_id, conversation_id


@contextmanager
def local_trusted_mcp_server() -> Iterator[str]:
    """启动只在本机监听的受信 Streamable HTTP MCP Server"""
    mcp_server = MCPServer("deep-researcher-test-mcp")
    records: dict[str, dict[str, str]] = {}

    @mcp_server.tool(name="research_notes.create", structured_output=True)
    def create_research_note(title: str, idempotency_key: str) -> dict[str, str]:
        """按稳定幂等键创建本地研究记录"""
        records.setdefault(
            idempotency_key,
            {"summary": f"已创建研究记录：{title}"},
        )
        return records[idempotency_key]

    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host="127.0.0.1",
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = Server(Config(app, log_level="warning", lifespan="on"))
    thread = Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="trusted-mcp-test-server",
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("本地受信 MCP Server 启动超时")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def test_rejecting_high_risk_tool_call_resumes_run_without_side_effect(tmp_path) -> None:
    """验证高风险工具拒绝后研究继续且没有副作用记录"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=RejectIfCalledMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "reject-high-risk-tool"},
            json={"content": "请使用受信工具记录研究主题：季度风险"},
        )

        assert app.state.run_worker.run_once() is True
        pending_run = client.get(
            f"/api/v1/conversations/{conversation_id}/active-run", headers=headers
        )
        approvals = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-approvals", headers=headers
        )

        approval = approvals.json()["items"][0]
        rejected = client.post(
            f"/api/v1/tool-approvals/{approval['id']}/reject", headers=headers
        )
        assert app.state.run_worker.run_once() is True

        events = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/events", headers=headers
        )
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        )
        tool_runs = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-runs", headers=headers
        )

    assert created.status_code == 202
    assert pending_run.json()["status"] == "waiting_approval"
    assert approvals.status_code == 200
    assert approval["tool_name"] == "research_notes.create"
    assert approval["safe_summary"] == "创建研究记录：季度风险"
    assert len(approval["parameters_hash"]) == 64
    assert "arguments" not in approval
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert detail.json()["status"] == "completed"
    assert tool_runs.json()["items"] == []
    assert "event: tool_approval_requested" in events.text
    assert "event: tool_call_rejected" in events.text
    assert "event: run_completed" in events.text


def test_mcp_effective_tools_require_skill_and_agent_intersection(tmp_path) -> None:
    """验证 Skill 启用后未授权写工具不会进入审批或工具执行"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=MultiToolMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        workspace_id, conversation_id = create_workspace_and_conversation(client, headers)
        enabled_mcp = client.post(
            f"/api/v1/workspaces/{workspace_id}/mcp/local-trusted/enable",
            headers=headers,
        )
        before_skill = client.get(
            f"/api/v1/conversations/{conversation_id}/mcp/local-trusted/tools",
            headers=headers,
        )
        client.post("/api/v1/skills/source-comparison/install", headers=headers)
        client.post(
            f"/api/v1/workspaces/{workspace_id}/skills/source-comparison/enable",
            headers=headers,
        )
        after_skill = client.get(
            f"/api/v1/conversations/{conversation_id}/mcp/local-trusted/tools",
            headers=headers,
        )
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "skill-tool-intersection"},
            json={"content": "请使用受信工具记录研究主题：受限工具"},
        ).json()
        assert app.state.run_worker.run_once() is True
        approvals = client.get(
            f"/api/v1/runs/{created['run_id']}/tool-approvals", headers=headers
        )
        tool_runs = client.get(
            f"/api/v1/runs/{created['run_id']}/tool-runs", headers=headers
        )
        events = client.get(
            f"/api/v1/runs/{created['run_id']}/events", headers=headers
        )

    assert before_skill.status_code == 200
    assert enabled_mcp.status_code == 200
    assert [item["name"] for item in before_skill.json()["items"]] == [
        "research_notes.create"
    ]
    assert after_skill.status_code == 200
    assert after_skill.json()["items"] == []
    assert approvals.json()["items"] == []
    assert tool_runs.json()["items"] == []
    assert "event: tool_approval_requested" not in events.text


def test_waiting_approval_event_stream_returns_after_current_events(tmp_path) -> None:
    """验证等待审批时 SSE 返回当前事件并允许 UI 展示审批"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=RejectIfCalledMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "waiting-approval-sse"},
            json={"content": "请使用受信工具记录研究主题：审批等待态"},
        ).json()
        assert app.state.run_worker.run_once() is True

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.get,
                f"/api/v1/runs/{created['run_id']}/events",
                headers=headers,
            )
            try:
                events = future.result(timeout=1)
            except FutureTimeoutError:
                client.post(f"/api/v1/runs/{created['run_id']}/cancel", headers=headers)
                future.result(timeout=2)
                raise AssertionError("等待审批时 SSE 未在发送当前事件后返回") from None

    assert events.status_code == 200
    assert "event: tool_approval_requested" in events.text
    assert "event: run_completed" not in events.text


def test_approving_exact_tool_call_executes_once_across_retries(tmp_path) -> None:
    """验证批准精确参数后只形成一次工具执行事实"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=SingleUseMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "approve-high-risk-tool"},
            json={"content": "请使用受信工具记录研究主题：供应链风险"},
        )

        assert app.state.run_worker.run_once() is True
        approval = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-approvals", headers=headers
        ).json()["items"][0]
        first_approval = client.post(
            f"/api/v1/tool-approvals/{approval['id']}/approve", headers=headers
        )
        repeated_approval = client.post(
            f"/api/v1/tool-approvals/{approval['id']}/approve", headers=headers
        )

        assert app.state.run_worker.run_once() is True
        after_completion = client.post(
            f"/api/v1/tool-approvals/{approval['id']}/approve", headers=headers
        )
        no_second_claim = app.state.run_worker.run_once()
        first_events = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/events", headers=headers
        )
        replayed_events = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/events",
            headers={**headers, "Last-Event-ID": "1"},
        )
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        )
        tool_runs = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-runs", headers=headers
        )

    assert first_approval.status_code == 200
    assert repeated_approval.status_code == 200
    assert after_completion.status_code == 200
    assert first_approval.json()["status"] == "approved"
    assert repeated_approval.json()["status"] == "approved"
    assert no_second_claim is False
    assert detail.json()["status"] == "completed"
    assert tool_runs.json()["items"] == [
        {
            "id": tool_runs.json()["items"][0]["id"],
            "tool_call_id": approval["tool_call_id"],
            "tool_name": "research_notes.create",
            "status": "completed",
            "result_summary": "已创建研究记录：供应链风险",
            "error_summary": None,
        }
    ]
    assert first_events.text.count("event: tool_call_completed") == 1
    assert replayed_events.text.count("event: tool_call_completed") == 1
    assert "event: run_completed" in first_events.text


def test_failed_tool_call_closes_tool_run_without_leaking_downstream_details(tmp_path) -> None:
    """验证 MCP 调用失败会形成安全的 Tool Run 失败终态"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=FailingMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "failed-tool-run"},
            json={"content": "请使用受信工具记录研究主题：失败收口"},
        ).json()
        assert app.state.run_worker.run_once() is True
        approval = client.get(
            f"/api/v1/runs/{created['run_id']}/tool-approvals", headers=headers
        ).json()["items"][0]
        client.post(
            f"/api/v1/tool-approvals/{approval['id']}/approve", headers=headers
        )
        assert app.state.run_worker.run_once() is True
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        )
        tool_runs = client.get(
            f"/api/v1/runs/{created['run_id']}/tool-runs", headers=headers
        )

    assert detail.json()["status"] == "failed"
    assert tool_runs.json()["items"][0]["status"] == "failed"
    assert tool_runs.json()["items"][0]["result_summary"] is None
    assert tool_runs.json()["items"][0]["error_summary"] == "MCP 工具调用失败"
    assert "downstream-secret-detail" not in tool_runs.text


@pytest.mark.parametrize(
    ("error", "safe_summary"),
    [
        (McpTransportError("private transport detail"), "MCP 传输失败"),
        (McpProtocolError("private protocol detail"), "MCP 协议失败"),
        (McpToolError("private tool detail"), "MCP 工具返回失败"),
        (McpResultValidationError("private result detail"), "MCP 结果未通过校验"),
    ],
)
def test_tool_run_exposes_only_safe_mcp_failure_category(
    tmp_path, error: Exception, safe_summary: str
) -> None:
    """验证公共 Tool Run 只公开 MCP 失败分类而不公开下游详情"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=CategorizedFailingMcpGateway(error),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": f"categorized-{type(error).__name__}"},
            json={"content": f"请使用受信工具记录研究主题：{safe_summary}"},
        ).json()
        assert app.state.run_worker.run_once() is True
        approval = client.get(
            f"/api/v1/runs/{created['run_id']}/tool-approvals", headers=headers
        ).json()["items"][0]
        client.post(
            f"/api/v1/tool-approvals/{approval['id']}/approve", headers=headers
        )
        assert app.state.run_worker.run_once() is True
        tool_runs = client.get(
            f"/api/v1/runs/{created['run_id']}/tool-runs", headers=headers
        )

    assert tool_runs.json()["items"][0]["status"] == "failed"
    assert tool_runs.json()["items"][0]["error_summary"] == safe_summary
    assert str(error) not in tool_runs.text


def test_approved_tool_call_uses_local_trusted_http_mcp_adapter(tmp_path) -> None:
    """验证批准后的公共运行会通过本机 MCP Adapter 形成执行结果"""
    with local_trusted_mcp_server() as mcp_url:
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            object_store_root=tmp_path / "objects",
            trusted_mcp_url=mcp_url,
        )
        app = create_app(settings, embedded_worker=False)

        with TestClient(app) as client:
            headers = register(client)
            conversation_id = create_conversation(client, headers)
            created = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={**headers, "Idempotency-Key": "local-http-mcp"},
                json={"content": "请使用受信工具记录研究主题：本地 MCP 验收"},
            )
            assert app.state.run_worker.run_once() is True
            approval = client.get(
                f"/api/v1/runs/{created.json()['run_id']}/tool-approvals",
                headers=headers,
            ).json()["items"][0]
            approved = client.post(
                f"/api/v1/tool-approvals/{approval['id']}/approve", headers=headers
            )
            assert app.state.run_worker.run_once() is True
            events = client.get(
                f"/api/v1/runs/{created.json()['run_id']}/events", headers=headers
            )
            tool_runs = client.get(
                f"/api/v1/runs/{created.json()['run_id']}/tool-runs", headers=headers
            )

    assert approved.status_code == 200
    assert tool_runs.json()["items"][0]["status"] == "completed"
    assert tool_runs.json()["items"][0]["result_summary"] == (
        "已创建研究记录：本地 MCP 验收"
    )
    assert "event: tool_call_completed" in events.text
    assert "event: run_completed" in events.text


def test_expired_tool_approval_resumes_without_side_effect(tmp_path) -> None:
    """验证审批过期后研究继续且不形成工具执行记录"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
        tool_approval_ttl_seconds=0,
    )
    app = create_app(
        settings,
        mcp_gateway=RejectIfCalledMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "expired-tool-approval"},
            json={"content": "请使用受信工具记录研究主题：过期审批"},
        )
        assert app.state.run_worker.run_once() is True
        approval = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-approvals", headers=headers
        ).json()["items"][0]
        expired = client.post(
            f"/api/v1/tool-approvals/{approval['id']}/approve", headers=headers
        )
        assert app.state.run_worker.run_once() is True
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        )
        tool_runs = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-runs", headers=headers
        )

    assert expired.json()["status"] == "expired"
    assert detail.json()["status"] == "completed"
    assert tool_runs.json()["items"] == []


def test_cancelling_waiting_approval_invalidates_call_without_side_effect(tmp_path) -> None:
    """验证等待审批时取消会立即失效调用且不再恢复 Worker"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=RejectIfCalledMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        conversation_id = create_conversation(client, headers)
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "cancel-waiting-approval"},
            json={"content": "请使用受信工具记录研究主题：取消审批"},
        )
        assert app.state.run_worker.run_once() is True
        cancelled = client.post(
            f"/api/v1/runs/{created.json()['run_id']}/cancel", headers=headers
        )
        no_resume = app.state.run_worker.run_once()
        approvals = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-approvals", headers=headers
        )
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        )
        tool_runs = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-runs", headers=headers
        )
        ledger = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/ledger", headers=headers
        )

    assert cancelled.json()["status"] == "cancelled"
    assert no_resume is False
    assert approvals.json()["items"][0]["status"] == "cancelled"
    assert detail.json()["status"] == "cancelled"
    assert tool_runs.json()["items"] == []
    assert ledger.json()["status"] == "cancelled"
    assert ledger.json()["coverage"] == {
        "citation_count": 0,
        "verified_claim_count": 0,
        "complete": False,
    }
    assert ledger.json()["gaps"][0]["status"] == "open"
    assert ledger.json()["stop_decision"] == {
        "reason": "cancelled",
        "completeness": "partial",
    }


def test_disabling_workspace_mcp_invalidates_pending_and_new_calls(tmp_path) -> None:
    """验证 Workspace 停用后等待调用失效且新运行不再请求审批"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        mcp_gateway=RejectIfCalledMcpGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers = register(client)
        workspace_id, conversation_id = create_workspace_and_conversation(client, headers)
        enabled = client.post(
            f"/api/v1/workspaces/{workspace_id}/mcp/local-trusted/enable", headers=headers
        )
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "disable-pending-tool"},
            json={"content": "请使用受信工具记录研究主题：停用传播"},
        )
        assert app.state.run_worker.run_once() is True
        disabled = client.post(
            f"/api/v1/workspaces/{workspace_id}/mcp/local-trusted/disable", headers=headers
        )
        assert app.state.run_worker.run_once() is True
        pending_approvals = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-approvals", headers=headers
        )
        first_tool_runs = client.get(
            f"/api/v1/runs/{created.json()['run_id']}/tool-runs", headers=headers
        )

        second = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "disabled-new-tool"},
            json={"content": "请使用受信工具记录研究主题：停用后的新运行"},
        )
        assert app.state.run_worker.run_once() is True
        new_approvals = client.get(
            f"/api/v1/runs/{second.json()['run_id']}/tool-approvals", headers=headers
        )
        second_detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        )

    assert enabled.status_code == 200
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert pending_approvals.json()["items"][0]["status"] == "disabled"
    assert first_tool_runs.json()["items"] == []
    assert new_approvals.json()["items"] == []
    assert second_detail.json()["status"] == "completed"
