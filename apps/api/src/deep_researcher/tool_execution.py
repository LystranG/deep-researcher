import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import NotRequired, Protocol, TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import (
    ResearchRun,
    ToolApproval,
    ToolCall,
    ToolRun,
    WorkspaceMcpGrant,
)


class McpToolDescriptor(TypedDict):
    """描述受信 MCP Adapter 公开的单个工具"""

    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: NotRequired[dict[str, object] | None]
    annotations: NotRequired[dict[str, object] | None]


AGENT_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "researcher": frozenset({"document_search", "web_search", "research_notes.create"}),
    "verifier": frozenset({"citation_read"}),
    "writer": frozenset(),
}


class McpGateway(Protocol):
    """隔离领域模块与具体 MCP SDK 的小接口"""

    async def list_tools(self) -> list[McpToolDescriptor]:
        """发现当前受信服务器允许使用的工具"""

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        """调用一个已通过领域授权的 MCP 工具"""


class DisabledMcpGateway:
    """在未配置受信 MCP 时显式关闭工具能力"""

    async def list_tools(self) -> list[McpToolDescriptor]:
        """返回空工具目录"""
        return []

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        """拒绝未配置服务器时的工具调用"""
        raise RuntimeError("MCP 工具能力未配置")


class ToolCallSnapshot(TypedDict):
    """进入 Graph checkpoint 的非敏感工具调用快照"""

    id: str
    approval_id: str
    tool_name: str
    parameters_hash: str
    safe_summary: str
    expires_at: str


class ToolOutcome(TypedDict):
    """Graph 恢复后可投影的工具业务结果"""

    tool_call_id: str
    status: str
    result_summary: str | None


class ToolApprovalError(RuntimeError):
    """表示审批不存在、失效或不再允许变更"""


class McpGatewayError(RuntimeError):
    """表示可映射为固定安全摘要的 MCP Gateway 失败"""

    safe_summary = "MCP 工具调用失败"


class McpTransportError(McpGatewayError):
    """表示 MCP HTTP 连接或传输失败"""

    safe_summary = "MCP 传输失败"


class McpProtocolError(McpGatewayError):
    """表示 MCP 初始化或协议消息失败"""

    safe_summary = "MCP 协议失败"


class McpToolError(McpGatewayError):
    """表示 MCP Server 返回工具业务失败"""

    safe_summary = "MCP 工具返回失败"


class McpResultValidationError(McpGatewayError):
    """表示 MCP 工具结果不符合领域摘要契约"""

    safe_summary = "MCP 结果未通过校验"


class ToolExecutionService:
    """封装调用规范化、一次性审批和副作用幂等边界"""

    _PREFIX = "请使用受信工具记录研究主题："
    _TOOL_NAME = "research_notes.create"
    _PLUGIN_SLUG = "local-trusted"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        gateway: McpGateway,
        *,
        approval_ttl_seconds: int = 900,
    ) -> None:
        """创建工具执行模块并配置审批有效期"""
        self._session_factory = session_factory
        self._gateway = gateway
        self._gateway_enabled = not isinstance(gateway, DisabledMcpGateway)
        self._approval_ttl_seconds = approval_ttl_seconds
        self._lock = Lock()

    async def prepare(
        self,
        run_id: UUID,
        question: str,
        *,
        allowed_tools: frozenset[str] | None = None,
    ) -> ToolCallSnapshot | None:
        """规范化高风险调用并幂等创建待审批事实"""
        intent = await self._normalize_intent(run_id, question, allowed_tools)
        if intent is None:
            return None
        tool_name, parameters_hash, safe_summary = intent
        now = datetime.now(UTC)
        with self._lock, self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            if run is None or run.initiated_by_user_id is None:
                raise RuntimeError("研究运行不存在或缺少发起用户")
            if run.cancel_requested_at is not None or run.status in {
                "cancelled",
                "completed",
                "partial",
                "failed",
            }:
                return None
            grant = session.scalar(
                select(WorkspaceMcpGrant).where(
                    WorkspaceMcpGrant.workspace_id == run.workspace_id,
                    WorkspaceMcpGrant.plugin_slug == self._PLUGIN_SLUG,
                )
            )
            if grant is None and self._gateway_enabled:
                grant = WorkspaceMcpGrant(
                    workspace_id=run.workspace_id,
                    plugin_slug=self._PLUGIN_SLUG,
                    enabled=True,
                )
                session.add(grant)
                session.flush()
            if grant is None or not grant.enabled:
                return None
            tool_call = session.scalar(
                select(ToolCall).where(
                    ToolCall.run_id == run.id,
                    ToolCall.tool_name == tool_name,
                    ToolCall.parameters_hash == parameters_hash,
                )
            )
            if tool_call is None:
                tool_call = ToolCall(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    tool_name=tool_name,
                    risk_level="write",
                    parameters_hash=parameters_hash,
                    safe_summary=safe_summary,
                    status="pending_approval",
                )
                session.add(tool_call)
                session.flush()
            approval = session.scalar(
                select(ToolApproval).where(ToolApproval.tool_call_id == tool_call.id)
            )
            if approval is None:
                approval = ToolApproval(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    tool_call_id=tool_call.id,
                    requested_for_user_id=run.initiated_by_user_id,
                    parameters_hash=parameters_hash,
                    status="pending",
                    expires_at=now + timedelta(seconds=self._approval_ttl_seconds),
                )
                session.add(approval)
                session.flush()
            run.status = "waiting_approval"
            return self._snapshot(tool_call, approval)

    def pending_resume(self, run_id: UUID) -> dict[str, str] | None:
        """读取已决审批并构造不含参数的 Graph 恢复值"""
        with self._session_factory() as session:
            row = session.execute(
                select(ToolApproval, ToolCall)
                .join(ToolCall, ToolCall.id == ToolApproval.tool_call_id)
                .where(
                    ToolApproval.run_id == run_id,
                    ToolApproval.status.in_(
                        {"approved", "rejected", "expired", "disabled"}
                    ),
                    ToolCall.status.in_(
                        {"approved", "rejected", "expired", "disabled"}
                    ),
                )
            ).first()
            if row is None:
                return None
            approval, tool_call = row
            return {
                "approval_id": str(approval.id),
                "tool_call_id": str(tool_call.id),
                "status": approval.status,
                "parameters_hash": approval.parameters_hash,
                "user_id": str(approval.decided_by_user_id or approval.requested_for_user_id),
            }

    def decide(self, approval_id: UUID, user_id: UUID, decision: str) -> ToolApproval:
        """按用户、参数哈希和有效期写入一次性审批决定"""
        if decision not in {"approved", "rejected"}:
            raise ToolApprovalError("审批决定无效")
        now = datetime.now(UTC)
        with self._lock, self._session_factory.begin() as session:
            approval = session.scalar(
                select(ToolApproval)
                .where(ToolApproval.id == approval_id)
                .with_for_update()
            )
            if approval is None or approval.requested_for_user_id != user_id:
                raise ToolApprovalError("工具审批不存在")
            tool_call = session.get(ToolCall, approval.tool_call_id)
            run = session.scalar(
                select(ResearchRun)
                .where(ResearchRun.id == approval.run_id)
                .with_for_update()
            )
            if tool_call is None or run is None:
                raise ToolApprovalError("工具审批关联的研究运行不存在")
            if approval.status != "pending":
                if approval.status == decision:
                    return approval
                raise ToolApprovalError("工具审批已经完成")
            grant_enabled = session.scalar(
                select(WorkspaceMcpGrant.enabled).where(
                    WorkspaceMcpGrant.workspace_id == approval.workspace_id,
                    WorkspaceMcpGrant.plugin_slug == self._PLUGIN_SLUG,
                )
            )
            if run.cancel_requested_at is not None:
                approval.status = "cancelled"
                tool_call.status = "cancelled"
                run.status = "cancelled"
            elif decision == "approved" and not grant_enabled:
                approval.status = "disabled"
                tool_call.status = "disabled"
            elif approval.expires_at.replace(tzinfo=UTC) <= now:
                approval.status = "expired"
                tool_call.status = "expired"
            else:
                approval.status = decision
                tool_call.status = decision
            approval.decided_by_user_id = user_id
            approval.decided_at = now
            if run.cancel_requested_at is None:
                run.status = "queued"
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = None
            session.flush()
            session.expunge(approval)
            return approval

    async def execute(
        self,
        run_id: UUID,
        question: str,
        resume: dict[str, str],
        *,
        allowed_tools: frozenset[str] | None = None,
    ) -> ToolOutcome:
        """校验恢复值并在 Tool Run 幂等边界内执行一次副作用"""
        intent = await self._normalize_intent(run_id, question, allowed_tools)
        if intent is None:
            raise RuntimeError("恢复时无法重建工具调用意图")
        tool_name, parameters_hash, _ = intent
        if parameters_hash != resume.get("parameters_hash"):
            raise RuntimeError("审批参数哈希与恢复调用不一致")
        tool_call_id = UUID(resume["tool_call_id"])
        approval_id = UUID(resume["approval_id"])
        with self._lock, self._session_factory.begin() as session:
            tool_call = session.scalar(
                select(ToolCall).where(
                    ToolCall.id == tool_call_id,
                    ToolCall.run_id == run_id,
                    ToolCall.tool_name == tool_name,
                    ToolCall.parameters_hash == parameters_hash,
                ).with_for_update()
            )
            approval = session.scalar(
                select(ToolApproval).where(
                    ToolApproval.id == approval_id,
                    ToolApproval.tool_call_id == tool_call_id,
                    ToolApproval.run_id == run_id,
                    ToolApproval.parameters_hash == parameters_hash,
                )
            )
            if tool_call is None or approval is None:
                raise RuntimeError("工具调用或审批不存在")
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            grant_enabled = session.scalar(
                select(WorkspaceMcpGrant.enabled).where(
                    WorkspaceMcpGrant.workspace_id == tool_call.workspace_id,
                    WorkspaceMcpGrant.plugin_slug == self._PLUGIN_SLUG,
                )
            )
            if run is None or run.cancel_requested_at is not None:
                return {
                    "tool_call_id": str(tool_call.id),
                    "status": "cancelled",
                    "result_summary": None,
                }
            resume_user_id = UUID(resume["user_id"])
            if (
                approval.requested_for_user_id != resume_user_id
                or approval.decided_by_user_id != resume_user_id
            ):
                raise RuntimeError("审批用户与恢复命令不一致")
            if approval.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
                approval.status = "expired"
                tool_call.status = "expired"
                return {
                    "tool_call_id": str(tool_call.id),
                    "status": "expired",
                    "result_summary": None,
                }
            if not grant_enabled:
                return {
                    "tool_call_id": str(tool_call.id),
                    "status": "disabled",
                    "result_summary": None,
                }
            if approval.status != resume.get("status"):
                raise RuntimeError("审批状态与恢复命令不一致")
            if approval.status != "approved":
                return {
                    "tool_call_id": str(tool_call.id),
                    "status": approval.status,
                    "result_summary": None,
                }
            existing_run = session.scalar(
                select(ToolRun).where(ToolRun.tool_call_id == tool_call.id)
            )
            if existing_run is not None:
                return {
                    "tool_call_id": str(tool_call.id),
                    "status": existing_run.status,
                    "result_summary": existing_run.result_summary,
                }
            tool_run = ToolRun(
                workspace_id=tool_call.workspace_id,
                run_id=run_id,
                tool_call_id=tool_call.id,
                status="running",
            )
            session.add(tool_run)
            session.flush()
            tool_run_id = tool_run.id

        arguments = self._arguments(run_id, question)
        with self._lock, self._session_factory.begin() as session:
            run = session.get(ResearchRun, run_id)
            tool_call = session.get(ToolCall, tool_call_id)
            approval = session.get(ToolApproval, approval_id)
            if (
                run is None
                or tool_call is None
                or approval is None
                or run.cancel_requested_at is not None
                or (allowed_tools is not None and tool_name not in allowed_tools)
                or approval.status != "approved"
                or approval.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
            ):
                return {
                    "tool_call_id": str(tool_call_id),
                    "status": (
                        "cancelled"
                        if run is None or run.cancel_requested_at is not None
                        else "disabled"
                    ),
                    "result_summary": None,
                }
            grant_enabled = session.scalar(
                select(WorkspaceMcpGrant.enabled).where(
                    WorkspaceMcpGrant.workspace_id == tool_call.workspace_id,
                    WorkspaceMcpGrant.plugin_slug == self._PLUGIN_SLUG,
                )
            )
            if not grant_enabled:
                return {
                    "tool_call_id": str(tool_call_id),
                    "status": "disabled",
                    "result_summary": None,
                }
        try:
            result = await self._gateway.call_tool(tool_name, arguments)
        except Exception as exc:
            error_summary = str(
                getattr(exc, "safe_summary", "MCP 工具调用失败")
            )[:1000]
            with self._lock, self._session_factory.begin() as session:
                failed_tool_run = session.get(ToolRun, tool_run_id)
                failed_tool_call = session.get(ToolCall, tool_call_id)
                if failed_tool_run is not None and failed_tool_run.status == "running":
                    failed_tool_run.status = "failed"
                    failed_tool_run.error_summary = error_summary
                    failed_tool_run.completed_at = datetime.now(UTC)
                if failed_tool_call is not None:
                    failed_tool_call.status = "failed"
            raise RuntimeError(error_summary) from exc
        result_summary = str(result.get("summary") or "工具执行完成")[:2000]
        with self._lock, self._session_factory.begin() as session:
            completed_tool_run = session.get(ToolRun, tool_run_id)
            tool_call = session.get(ToolCall, tool_call_id)
            run = session.scalar(
                select(ResearchRun)
                .where(ResearchRun.id == run_id)
                .with_for_update()
            )
            if completed_tool_run is None or tool_call is None or run is None:
                raise RuntimeError("工具执行记录不存在")
            if run.cancel_requested_at is not None or run.status == "cancelled":
                completed_tool_run.status = "cancelled"
                completed_tool_run.error_summary = "研究运行已取消"
                completed_tool_run.completed_at = datetime.now(UTC)
                tool_call.status = "cancelled"
                return {
                    "tool_call_id": str(tool_call_id),
                    "status": "cancelled",
                    "result_summary": None,
                }
            completed_tool_run.status = "completed"
            completed_tool_run.result_summary = result_summary
            completed_tool_run.completed_at = datetime.now(UTC)
            tool_call.status = "completed"
        return {
            "tool_call_id": str(tool_call_id),
            "status": "completed",
            "result_summary": result_summary,
        }

    def ensure_workspace(self, workspace_id: UUID) -> None:
        """为已配置的本地受信 MCP 建立默认 Workspace grant"""
        if not self._gateway_enabled:
            return
        with self._lock, self._session_factory.begin() as session:
            grant = session.scalar(
                select(WorkspaceMcpGrant).where(
                    WorkspaceMcpGrant.workspace_id == workspace_id,
                    WorkspaceMcpGrant.plugin_slug == self._PLUGIN_SLUG,
                )
            )
            if grant is None:
                session.add(
                    WorkspaceMcpGrant(
                        workspace_id=workspace_id,
                        plugin_slug=self._PLUGIN_SLUG,
                        enabled=True,
                    )
                )

    def set_workspace_enabled(self, workspace_id: UUID, enabled: bool) -> bool:
        """切换 Workspace grant，并让等待审批的调用立即失效"""
        if enabled and not self._gateway_enabled:
            raise ToolApprovalError("本地受信 MCP 未配置")
        with self._lock, self._session_factory.begin() as session:
            grant = session.scalar(
                select(WorkspaceMcpGrant).where(
                    WorkspaceMcpGrant.workspace_id == workspace_id,
                    WorkspaceMcpGrant.plugin_slug == self._PLUGIN_SLUG,
                )
            )
            if grant is None:
                grant = WorkspaceMcpGrant(
                    workspace_id=workspace_id,
                    plugin_slug=self._PLUGIN_SLUG,
                    enabled=enabled,
                )
                session.add(grant)
            else:
                grant.enabled = enabled
            if not enabled:
                rows = session.execute(
                    select(ToolApproval, ToolCall)
                    .join(ToolCall, ToolCall.id == ToolApproval.tool_call_id)
                    .where(
                        ToolApproval.workspace_id == workspace_id,
                        ToolApproval.status == "pending",
                    )
                ).all()
                for approval, tool_call in rows:
                    approval.status = "disabled"
                    tool_call.status = "disabled"
                    run = session.scalar(
                        select(ResearchRun)
                        .where(ResearchRun.id == approval.run_id)
                        .with_for_update()
                    )
                    if (
                        run is not None
                        and run.status == "waiting_approval"
                        and run.cancel_requested_at is None
                    ):
                        run.status = "queued"
                        run.lease_owner = None
                        run.lease_expires_at = None
                        run.heartbeat_at = None
            return enabled

    def cancel_pending(self, run_id: UUID) -> None:
        """让已取消运行中的待审批调用立即进入取消状态"""
        with self._lock, self._session_factory.begin() as session:
            rows = session.execute(
                select(ToolApproval, ToolCall)
                .join(ToolCall, ToolCall.id == ToolApproval.tool_call_id)
                .where(
                    ToolApproval.run_id == run_id,
                    ToolApproval.status == "pending",
                )
            ).all()
            for approval, tool_call in rows:
                approval.status = "cancelled"
                tool_call.status = "cancelled"

    async def _normalize_intent(
        self,
        run_id: UUID,
        question: str,
        allowed_tools: frozenset[str] | None = None,
    ) -> tuple[str, str, str] | None:
        """从明确用户表达生成受信示例工具的规范化意图"""
        if not question.startswith(self._PREFIX):
            return None
        title = question.removeprefix(self._PREFIX).strip()
        if not title:
            return None
        if allowed_tools is not None and self._TOOL_NAME not in allowed_tools:
            return None
        tools = await self._gateway.list_tools()
        if not any(tool["name"] == self._TOOL_NAME for tool in tools):
            return None
        arguments = self._arguments(run_id, question)
        canonical = json.dumps(
            {"tool_name": self._TOOL_NAME, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            self._TOOL_NAME,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            f"创建研究记录：{title}",
        )

    def _arguments(self, run_id: UUID, question: str) -> dict[str, object]:
        """按运行标识重建精确且可去重的受信工具参数"""
        return {
            "title": question.removeprefix(self._PREFIX).strip(),
            "idempotency_key": f"tool-call:{run_id}",
        }

    @staticmethod
    def effective_allowed_tools(
        *,
        agent_role: str,
        skill_allowed_tools: frozenset[str] | None,
    ) -> frozenset[str] | None:
        """计算系统、Agent 与 Skill 的有效工具交集"""
        agent_tools = AGENT_TOOL_ALLOWLIST.get(agent_role, frozenset())
        if skill_allowed_tools is None:
            return agent_tools
        return agent_tools.intersection(skill_allowed_tools)

    async def list_effective_tools(
        self,
        workspace_id: UUID,
        *,
        agent_role: str,
        skill_allowed_tools: frozenset[str] | None,
    ) -> list[McpToolDescriptor]:
        """读取 Workspace、Agent 与 Skill 交集后的工具目录"""
        if not self._gateway_enabled:
            return []
        with self._session_factory() as session:
            grant_enabled = session.scalar(
                select(WorkspaceMcpGrant.enabled).where(
                    WorkspaceMcpGrant.workspace_id == workspace_id,
                    WorkspaceMcpGrant.plugin_slug == self._PLUGIN_SLUG,
                )
            )
        if not grant_enabled:
            return []
        allowed_tools = self.effective_allowed_tools(
            agent_role=agent_role,
            skill_allowed_tools=skill_allowed_tools,
        )
        if not allowed_tools:
            return []
        return [
            tool
            for tool in await self._gateway.list_tools()
            if tool["name"] in allowed_tools
        ]

    def _snapshot(
        self, tool_call: ToolCall, approval: ToolApproval
    ) -> ToolCallSnapshot:
        """生成不包含原始参数的 Graph 安全快照"""
        return {
            "id": str(tool_call.id),
            "approval_id": str(approval.id),
            "tool_name": tool_call.tool_name,
            "parameters_hash": tool_call.parameters_hash,
            "safe_summary": tool_call.safe_summary,
            "expires_at": approval.expires_at.isoformat(),
        }
