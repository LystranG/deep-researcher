from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from deep_researcher.database import Base

PgVector: Any = None
try:
    from pgvector.sqlalchemy import Vector

    PgVector = Vector
except ImportError:  # pragma: no cover - installed in PostgreSQL deployments
    pass


class EmbeddingVector(TypeDecorator[list[float] | None]):
    """PostgreSQL 使用 pgvector，SQLite 测试使用 JSON 保存 embedding"""

    impl = JSON
    cache_ok = True
    if PgVector is not None:
        comparator_factory = PgVector.comparator_factory

    def __init__(self, dimensions: int | None = None) -> None:
        """初始化可选维度的 embedding 类型"""
        self.dimensions = dimensions
        super().__init__()

    def load_dialect_impl(self, dialect: Any) -> Any:
        """按数据库方言选择 pgvector 或 JSON 实现"""
        if dialect.name == "postgresql" and PgVector is not None:
            return dialect.type_descriptor(PgVector(self.dimensions))
        return dialect.type_descriptor(JSON())


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    instructions: Mapped[str | None] = mapped_column(Text, default=None)
    memory_auto_apply: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(240))
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (UniqueConstraint("message_id", "attachment_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    attachment_id: Mapped[UUID] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), index=True
    )


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (UniqueConstraint("conversation_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    initiated_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None
    )
    trigger_message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id"), unique=True)
    assistant_message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    next_event_seq: Mapped[int] = mapped_column(Integer, default=1)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(120), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    reservation_status: Mapped[str] = mapped_column(String(20), default="none")
    reserved_token_budget: Mapped[int] = mapped_column(Integer, default=0)
    reserved_cost_micros: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ResearchPlan(Base):
    """保存 Research Run 的不可变计划版本快照"""

    __tablename__ = "research_plans"
    __table_args__ = (UniqueConstraint("run_id", "version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_version: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(32), default="active")
    goal: Mapped[str] = mapped_column(Text)
    plan_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ResearchTask(Base):
    __tablename__ = "research_tasks"
    __table_args__ = (UniqueConstraint("run_id", "ordinal"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_plans.id", ondelete="CASCADE"), index=True, default=None
    )
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    ordinal: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    goal: Mapped[str] = mapped_column(Text, default="")
    success_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    dependencies: Mapped[list[int]] = mapped_column(JSON, default=list)
    local_budget: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    role: Mapped[str] = mapped_column(String(32), default="researcher")
    depth: Mapped[int] = mapped_column(Integer, default=1)
    token_budget: Mapped[int] = mapped_column(Integer, default=2000)
    time_budget_seconds: Mapped[int] = mapped_column(Integer, default=30)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    failure_impact: Mapped[str | None] = mapped_column(Text, default=None)
    lease_owner: Mapped[str | None] = mapped_column(String(120), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TaskClaim(Base):
    """保存每次 ResearchTask 领取的租约和单调 fencing epoch"""

    __tablename__ = "task_claims"
    __table_args__ = (UniqueConstraint("task_id", "fencing_epoch"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_tasks.id", ondelete="CASCADE"), index=True
    )
    lease_owner: Mapped[str] = mapped_column(String(120))
    fencing_epoch: Mapped[int] = mapped_column(Integer)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TaskOutcome(Base):
    """保存一个 ResearchTask 至多一次的不可变终态结果"""

    __tablename__ = "task_outcomes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_tasks.id", ondelete="CASCADE"), unique=True, index=True
    )
    fencing_epoch: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    outcome_ref: Mapped[str] = mapped_column(String(200), unique=True)
    result_reference: Mapped[str | None] = mapped_column(String(1000), default=None)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    failure_ref: Mapped[str | None] = mapped_column(String(1000), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(ResearchPlan, "before_update")
@event.listens_for(ResearchPlan, "before_delete")
def reject_research_plan_mutation(_mapper: object, _connection: object, _target: object) -> None:
    """Plan revisions are append-only business facts."""
    raise ValueError("ResearchPlan is immutable")


@event.listens_for(TaskOutcome, "before_update")
@event.listens_for(TaskOutcome, "before_delete")
def reject_task_outcome_mutation(_mapper: object, _connection: object, _target: object) -> None:
    """Task outcomes are append-only business facts."""
    raise ValueError("TaskOutcome is immutable")


class Todo(Base):
    """记录 Agent 在研究运行中驱动的可恢复工作项"""

    __tablename__ = "todos"
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    research_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_tasks.id", ondelete="SET NULL"), index=True, default=None
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    purpose: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(40), default="research")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(200))
    lease_owner: Mapped[str | None] = mapped_column(String(120), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    result_summary: Mapped[str | None] = mapped_column(Text, default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    sandbox_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sandbox_executions.id", ondelete="SET NULL"), index=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class QuotaPolicy(Base):
    """保存 Workspace 的模型用量上限与预留状态"""

    __tablename__ = "quota_policies"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True, index=True
    )
    token_limit: Mapped[int] = mapped_column(Integer)
    cost_limit_micros: Mapped[int] = mapped_column(Integer)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_cost_micros: Mapped[int] = mapped_column(Integer, default=0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    used_cost_micros: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class UsageLedger(Base):
    """记录研究运行的模型 token 与费用结果"""

    __tablename__ = "usage_ledger"
    __table_args__ = (UniqueConstraint("run_id", "call_kind"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_tasks.id", ondelete="SET NULL"), index=True, default=None
    )
    call_kind: Mapped[str] = mapped_column(String(32), default="model")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ToolCall(Base):
    """保存 Graph 提出的规范化工具调用意图"""

    __tablename__ = "tool_calls"
    __table_args__ = (UniqueConstraint("run_id", "tool_name", "parameters_hash"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(200))
    risk_level: Mapped[str] = mapped_column(String(32))
    parameters_hash: Mapped[str] = mapped_column(String(64))
    safe_summary: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ToolApproval(Base):
    """保存绑定精确调用参数的一次性用户授权决定"""

    __tablename__ = "tool_approvals"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="CASCADE"), unique=True, index=True
    )
    requested_for_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    parameters_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ToolRun(Base):
    """保存工具真实执行的幂等结果"""

    __tablename__ = "tool_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32))
    result_summary: Mapped[str | None] = mapped_column(String(2000), default=None)
    error_summary: Mapped[str | None] = mapped_column(String(1000), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class WorkspaceMcpGrant(Base):
    """保存本地受信 MCP 在 Workspace 的启用状态"""

    __tablename__ = "workspace_mcp_grants"
    __table_args__ = (UniqueConstraint("workspace_id", "plugin_slug"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    plugin_slug: Mapped[str] = mapped_column(String(120), default="local-trusted")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq"),
        Index("ix_run_events_run_id_event_key", "run_id", "event_key", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    event_key: Mapped[str | None] = mapped_column(String(200), default=None)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(1000), unique=True)
    mime_type: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="processing")
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    promoted_document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), index=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    source_attachment_id: Mapped[UUID] = mapped_column(ForeignKey("attachments.id"), unique=True)
    version: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(1000))
    mime_type: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourceChunk(Base):
    __tablename__ = "source_chunks"
    __table_args__ = (
        UniqueConstraint("attachment_id", "ordinal"),
        Index("ix_source_chunks_workspace_embedding_status", "workspace_id", "embedding_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, default=None
    )
    attachment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), index=True, default=None
    )
    document_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True, default=None
    )
    source_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="CASCADE"), index=True, default=None
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer, default=None)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), default=None)
    embedding_model: Mapped[str | None] = mapped_column(String(200), default=None)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, default=None)
    embedding_status: Mapped[str] = mapped_column(String(32), default="pending")
    embedding_error: Mapped[str | None] = mapped_column(Text, default=None)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ConversationSegment(Base):
    """保存可跨会话检索的确定性消息分段"""

    __tablename__ = "conversation_segments"
    __table_args__ = (
        UniqueConstraint("conversation_id", "ordinal"),
        Index(
            "ix_conversation_segments_workspace_embedding_status",
            "workspace_id",
            "embedding_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    first_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), index=True, default=None
    )
    last_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), index=True, default=None
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    visibility_scope: Mapped[str] = mapped_column(String(32), default="workspace")
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), default=None)
    embedding_model: Mapped[str | None] = mapped_column(String(200), default=None)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, default=None)
    embedding_status: Mapped[str] = mapped_column(String(32), default="pending")
    embedding_error: Mapped[str | None] = mapped_column(Text, default=None)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ResearchRecord(Base):
    """保存已经核验、可跨会话复用的不可变研究记录版本"""

    __tablename__ = "research_records"
    __table_args__ = (
        UniqueConstraint("workspace_id", "record_key", "version"),
        Index("ix_research_records_workspace_status", "workspace_id", "status"),
        Index(
            "ix_research_records_workspace_embedding_status",
            "workspace_id",
            "embedding_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_runs.id", ondelete="SET NULL"), index=True, default=None
    )
    claim_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_claims.id", ondelete="SET NULL"), index=True, default=None
    )
    record_key: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    claim_text: Mapped[str] = mapped_column(Text)
    claim_kind: Mapped[str] = mapped_column(String(32), default="fact")
    status: Mapped[str] = mapped_column(String(32), default="active")
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_records.id", ondelete="SET NULL"), index=True, default=None
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    evidence_refs: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), default=None)
    embedding_model: Mapped[str | None] = mapped_column(String(200), default=None)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, default=None)
    embedding_status: Mapped[str] = mapped_column(String(32), default="pending")
    embedding_error: Mapped[str | None] = mapped_column(Text, default=None)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ResearchClaim(Base):
    """保存研究运行中通过证据核验的主张"""

    __tablename__ = "research_claims"
    __table_args__ = (
        UniqueConstraint("run_id", "content_hash"),
        Index("ix_research_claims_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    claim_text: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(32), default="verified")
    status: Mapped[str] = mapped_column(String(32), default="verified")
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvidenceSpan(Base):
    """保存来源快照中可精确回读的独立证据片段"""

    __tablename__ = "evidence_spans"
    __table_args__ = (
        UniqueConstraint("run_id", "source_chunk_id", "start_offset", "end_offset"),
        Index("ix_evidence_spans_workspace_run", "workspace_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    source_chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_chunks.id", ondelete="CASCADE"), index=True
    )
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ResearchClaimEvidence(Base):
    """区分主张与证据片段之间的支持或反驳关系"""

    __tablename__ = "research_claim_evidence"
    __table_args__ = (
        UniqueConstraint("claim_id", "evidence_span_id", "relation"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    claim_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_claims.id", ondelete="CASCADE"), index=True
    )
    evidence_span_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_spans.id", ondelete="CASCADE"), index=True
    )
    relation: Mapped[str] = mapped_column(String(32), default="supports")


class ResearchLedger(Base):
    """保存单次 Research Run 的可恢复研究账本"""

    __tablename__ = "research_ledgers"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), unique=True, index=True
    )
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class CoverageSnapshot(Base):
    """记录账本在终态时的证据覆盖判断"""

    __tablename__ = "coverage_snapshots"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    ledger_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_ledgers.id", ondelete="CASCADE"), index=True
    )
    citation_count: Mapped[int] = mapped_column(Integer, default=0)
    verified_claim_count: Mapped[int] = mapped_column(Integer, default=0)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvidenceGap(Base):
    """记录研究目标尚未覆盖的证据缺口"""

    __tablename__ = "evidence_gaps"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    ledger_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_ledgers.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StopDecision(Base):
    """记录由覆盖快照和运行状态形成的停止裁决"""

    __tablename__ = "stop_decisions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    ledger_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_ledgers.id", ondelete="CASCADE"), unique=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(64))
    completeness: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(32), default="web")
    content_kind: Mapped[str] = mapped_column(String(32), default="search_snippet")
    ordinal: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(4000))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SourceMapWork(Base):
    """记录一次可恢复的有界 Source Chunk map work"""

    __tablename__ = "source_map_works"
    __table_args__ = (
        UniqueConstraint("run_id", "input_hash"),
        Index("ix_source_map_works_run_status", "run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    ledger_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_ledgers.id", ondelete="CASCADE"), index=True
    )
    source_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="CASCADE"), index=True
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    chunk_ids: Mapped[list[str]] = mapped_column(JSON)
    chunk_hashes: Mapped[list[str]] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    digest: Mapped[dict[str, object] | None] = mapped_column(JSON, default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class WebAcquisitionAttempt(Base):
    """保存一次网页正文 Adapter 获取尝试的审计事实"""

    __tablename__ = "web_acquisition_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "source_ordinal", "attempt_ordinal"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    source_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="SET NULL"), index=True, default=None
    )
    source_ordinal: Mapped[int] = mapped_column(Integer)
    attempt_ordinal: Mapped[int] = mapped_column(Integer)
    adapter_id: Mapped[str] = mapped_column(String(100))
    adapter_version: Mapped[str] = mapped_column(String(100))
    requested_url: Mapped[str] = mapped_column(String(4000))
    final_url: Mapped[str | None] = mapped_column(String(4000), default=None)
    status: Mapped[str] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    content_type: Mapped[str | None] = mapped_column(String(200), default=None)
    warning_category: Mapped[str | None] = mapped_column(String(100), default=None)
    error_category: Mapped[str | None] = mapped_column(String(100), default=None)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    completeness: Mapped[str] = mapped_column(String(32), default="none")
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = (
        UniqueConstraint("message_id", "label"),
        CheckConstraint(
            "(source_chunk_id IS NOT NULL) != (derived_evidence_id IS NOT NULL)",
            name="ck_citations_exactly_one_source",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    source_chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_chunks.id"), index=True, default=None
    )
    derived_evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("derived_evidence.id"), index=True, default=None
    )
    label: Mapped[int] = mapped_column(Integer)
    answer_start: Mapped[int] = mapped_column(Integer)
    answer_end: Mapped[int] = mapped_column(Integer)
    evidence_start: Mapped[int] = mapped_column(Integer)
    evidence_end: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, default=None
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, default=None
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), index=True, default=None
    )
    scope: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    risk_level: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), default=None)
    embedding_model: Mapped[str | None] = mapped_column(String(200), default=None)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, default=None)
    embedding_status: Mapped[str] = mapped_column(String(32), default="pending")
    embedding_error: Mapped[str | None] = mapped_column(Text, default=None)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class MemoryRevision(Base):
    __tablename__ = "memory_revisions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    change_reason: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MemoryConflict(Base):
    __tablename__ = "memory_conflicts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    old_memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    new_memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending")
    resolution: Mapped[str | None] = mapped_column(String(32), default=None)
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SandboxExecution(Base):
    __tablename__ = "sandbox_executions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None
    )
    purpose: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text)
    input_message_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_attachment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_evidence_span_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class DerivedEvidence(Base):
    __tablename__ = "derived_evidence"
    __table_args__ = (UniqueConstraint("sandbox_execution_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    sandbox_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("sandbox_executions.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(Text)
    code_hash: Mapped[str] = mapped_column(String(64))
    input_message_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_attachment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_evidence_span_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    stdout: Mapped[str] = mapped_column(Text)
    stdout_hash: Mapped[str] = mapped_column(String(64))
    result_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    sandbox_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("sandbox_executions.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(1000))
    storage_key: Mapped[str] = mapped_column(String(2000), unique=True)
    media_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class VerificationJob(Base):
    __tablename__ = "verification_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    message_version: Mapped[int] = mapped_column(Integer)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    selected_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    model_version: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VerificationClaim(Base):
    __tablename__ = "verification_claims"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("verification_jobs.id", ondelete="CASCADE"), index=True
    )
    claim_text: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)


class VerificationEvidence(Base):
    __tablename__ = "verification_evidence"
    __table_args__ = (UniqueConstraint("claim_id", "citation_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    claim_id: Mapped[UUID] = mapped_column(
        ForeignKey("verification_claims.id", ondelete="CASCADE"), index=True
    )
    citation_id: Mapped[UUID] = mapped_column(
        ForeignKey("citations.id", ondelete="CASCADE"), index=True
    )


class SkillPackage(Base):
    __tablename__ = "skill_packages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str] = mapped_column(String(200))
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_package_id", "version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    skill_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_packages.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(50))
    content_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SkillInstallation(Base):
    __tablename__ = "skill_installations"
    __table_args__ = (UniqueConstraint("user_id", "skill_package_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    skill_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_packages.id", ondelete="CASCADE"), index=True
    )
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkspaceSkillGrant(Base):
    __tablename__ = "workspace_skill_grants"
    __table_args__ = (UniqueConstraint("workspace_id", "skill_installation_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    skill_installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_installations.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationSkillOverride(Base):
    __tablename__ = "conversation_skill_overrides"
    __table_args__ = (UniqueConstraint("conversation_id", "skill_installation_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    skill_installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_installations.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
