import hashlib
import mimetypes
import os
import re
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

import anyio
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from deep_researcher.coordinator import ResearchCoordinator, encode_event_data
from deep_researcher.database import build_engine, build_session_factory, session_scope
from deep_researcher.document_processor import (
    ConversationSegmentProcessor,
    DocumentProcessor,
    MemoryIndexer,
    ResearchRecordIndexer,
)
from deep_researcher.graph import ResearchGraphRunner
from deep_researcher.mcp_adapter import LocalTrustedHttpMcpAdapter
from deep_researcher.model_gateway import ModelGateway, build_model_gateway
from deep_researcher.models import (
    Artifact,
    Attachment,
    AuthSession,
    Citation,
    Conversation,
    ConversationSegment,
    ConversationSkillOverride,
    CoverageSnapshot,
    Document,
    DocumentVersion,
    EvidenceGap,
    EvidenceSpan,
    Memory,
    MemoryConflict,
    MemoryRevision,
    Message,
    MessageAttachment,
    ResearchClaimEvidence,
    ResearchLedger,
    ResearchRecord,
    ResearchRun,
    ResearchTask,
    RunEvent,
    SandboxExecution,
    SkillInstallation,
    SkillPackage,
    SkillVersion,
    SourceChunk,
    SourceMapWork,
    SourceSnapshot,
    StopDecision,
    Todo,
    ToolApproval,
    ToolCall,
    ToolRun,
    UsageLedger,
    User,
    VerificationClaim,
    VerificationEvidence,
    VerificationJob,
    WebAcquisitionAttempt,
    Workspace,
    WorkspaceMcpGrant,
    WorkspaceMember,
    WorkspaceSkillGrant,
)
from deep_researcher.quota import QuotaService
from deep_researcher.retrieval import (
    EmbeddingGateway,
    HybridRetrieval,
    LiteLLMEmbeddingGateway,
    LiteLLMRerankGateway,
    LiteLLMTokenEstimator,
    PostgresConversationSegmentRetrievalAdapter,
    PostgresMemoryRetrievalAdapter,
    PostgresResearchRecordRetrievalAdapter,
    PostgresSourceChunkRetrievalAdapter,
    RerankGateway,
)
from deep_researcher.run_queue import RunQueue
from deep_researcher.sandbox import (
    DockerSandbox,
    SandboxInputMount,
    SandboxRequest,
    SandboxUnavailableError,
)
from deep_researcher.security import hash_access_token, hash_password, issue_access_token
from deep_researcher.settings import Settings
from deep_researcher.skills import SOURCE_COMPARISON_MANIFEST, manifest_hash, validate_manifest
from deep_researcher.source_map import missing_source_map_chunk_ids
from deep_researcher.storage import FileTooLargeError, LocalObjectStore
from deep_researcher.tool_execution import (
    DisabledMcpGateway,
    McpGateway,
    ToolApprovalError,
    ToolExecutionService,
)
from deep_researcher.web_page import (
    HttpWebPageGateway,
    JinaReaderWebPageAdapter,
    WebAcquisition,
    WebAcquisitionGateway,
)
from deep_researcher.web_search import WebSearchGateway, build_web_search_gateway
from deep_researcher.worker import RunWorker


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CurrentUserResponse(BaseModel):
    id: str
    email: str


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    instructions: str | None = Field(default=None, max_length=12000)
    memory_auto_apply: bool = True


class WorkspaceUpdateRequest(WorkspaceCreateRequest):
    pass


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    description: str | None
    instructions: str | None
    archived: bool
    memory_auto_apply: bool


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]


class WorkspaceDeletePreviewResponse(BaseModel):
    conversations: int
    documents: int
    memories: int
    artifacts: int


class WorkspaceDeleteRequest(BaseModel):
    confirm_name: str


class ConversationCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)


class ConversationUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)


class ConversationResponse(BaseModel):
    id: str
    workspace_id: str
    title: str
    archived: bool


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]


class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    version: int


class MessageListResponse(BaseModel):
    items: list[MessageResponse]


class RunCreatedResponse(BaseModel):
    run_id: str
    assistant_message_id: str
    status: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str


class ResearchTaskResponse(BaseModel):
    ordinal: int
    title: str
    status: str
    failure_impact: str | None
    role: str
    depth: int
    token_budget: int
    time_budget_seconds: int
    allowed_tools: list[str]


class ModelUsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float


class RunDetailResponse(RunStatusResponse):
    tasks: list[ResearchTaskResponse]
    usage: ModelUsageResponse | None


class LedgerCoverageResponse(BaseModel):
    citation_count: int
    verified_claim_count: int
    complete: bool


class LedgerGapResponse(BaseModel):
    id: str
    description: str
    status: str


class LedgerStopDecisionResponse(BaseModel):
    reason: str
    completeness: str


class ResearchLedgerResponse(BaseModel):
    id: str
    run_id: str
    goal: str
    status: str
    coverage: LedgerCoverageResponse | None
    gaps: list[LedgerGapResponse]
    stop_decision: LedgerStopDecisionResponse | None
    missing_chunk_ids: list[str]


class SourceMapWorkResponse(BaseModel):
    """公开可审计的 bounded map work 与派生结果"""

    id: str
    source_snapshot_id: str
    snapshot_hash: str
    chunk_ids: list[str]
    chunk_hashes: list[str]
    input_hash: str
    prompt_version: str
    status: str
    digest: dict[str, object] | None
    failure_reason: str | None


class SourceMapWorkListResponse(BaseModel):
    """公开 Research Run 的有限 map work 列表"""

    items: list[SourceMapWorkResponse]


class TodoResponse(BaseModel):
    """公开单个 Agent Todo 的安全状态摘要"""

    id: str
    ordinal: int
    title: str
    purpose: str
    kind: str
    status: str
    result_summary: str | None
    failure_reason: str | None
    sandbox_execution_id: str | None
    sandbox_code: str | None
    sandbox_input_attachment_ids: list[str]
    sandbox_artifacts: list[dict[str, str | int]]


class TodoListResponse(BaseModel):
    """公开研究运行的 Todo 列表"""

    items: list[TodoResponse]


class AttachmentResponse(BaseModel):
    id: str
    conversation_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    status: str
    failure_reason: str | None


class DocumentResponse(BaseModel):
    id: str
    filename: str
    version: int
    sha256: str
    mime_type: str


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]


class ResearchRecordEvidenceResponse(BaseModel):
    id: str
    source_chunk_id: str
    start_offset: int
    end_offset: int
    source_hash: str


class ResearchRecordResponse(BaseModel):
    id: str
    record_key: str
    version: int
    claim_text: str
    status: str
    embedding_model: str | None
    embedding_status: str
    embedding_error: str | None
    evidence: list[ResearchRecordEvidenceResponse]


class ResearchRecordListResponse(BaseModel):
    items: list[ResearchRecordResponse]


class CitationResponse(BaseModel):
    id: str
    label: int
    source_type: str
    filename: str
    source_url: str | None
    source_captured_at: datetime | None
    document_version: int | None
    page_number: int | None
    evidence_text: str
    source_hash: str


class CitationListResponse(BaseModel):
    items: list[CitationResponse]


class WebAcquisitionAttemptResponse(BaseModel):
    adapter_id: str
    adapter_version: str
    requested_url: str
    final_url: str | None
    status: str
    http_status: int | None
    content_type: str | None
    warning_category: str | None
    error_category: str | None
    retryable: bool
    completeness: str
    truncated: bool
    content_hash: str | None
    selected_for_snapshot: bool


class ResearchSourceResponse(BaseModel):
    id: str
    ordinal: int
    title: str
    url: str
    content_kind: str
    captured_at: datetime
    content_preview: str
    content_hash: str
    acquisition_attempts: list[WebAcquisitionAttemptResponse]


class ResearchSourceListResponse(BaseModel):
    items: list[ResearchSourceResponse]


class ResearchSourceDetailResponse(ResearchSourceResponse):
    content: str


class MemoryCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    scope: str
    category: str = Field(min_length=1, max_length=32)
    risk_level: str
    conversation_id: UUID | None = None
    expires_at: datetime | None = None


class MemoryUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    expires_at: datetime | None = None


class MemoryResponse(BaseModel):
    id: str
    workspace_id: str
    conversation_id: str | None
    scope: str
    category: str
    risk_level: str
    content: str
    status: str
    expires_at: datetime | None
    source_message_id: str | None
    conflict: "MemoryConflictResponse | None" = None


class MemoryConflictResponse(BaseModel):
    id: str
    old_memory_id: str
    old_content: str
    new_memory_id: str
    new_content: str
    status: str
    resolution: str | None


class MemoryRevisionResponse(BaseModel):
    content: str
    status: str
    change_reason: str
    created_at: datetime


class MemoryDetailResponse(MemoryResponse):
    revisions: list[MemoryRevisionResponse]


class MemoryConflictResolveRequest(BaseModel):
    action: str


class SandboxExecutionCreateRequest(BaseModel):
    purpose: str = Field(min_length=1, max_length=4000)
    code: str = Field(min_length=1, max_length=100_000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=20)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class ArtifactResponse(BaseModel):
    id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class SandboxExecutionResponse(BaseModel):
    id: str
    run_id: str
    purpose: str
    code: str
    attachment_ids: list[str]
    timeout_seconds: int
    status: str
    stdout: str
    stderr: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    artifacts: list[ArtifactResponse]


class EvidenceCheckRequest(BaseModel):
    message_version: int = Field(ge=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=20_000)


class EvidenceCheckResponse(BaseModel):
    id: str
    claim: str
    verdict: str
    reason: str
    evidence: list[CitationResponse]
    model_version: str
    disclaimer: str


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]


class SkillResponse(BaseModel):
    slug: str
    name: str
    description: str
    publisher: str
    version: str
    content_hash: str
    manifest: dict[str, object]
    installed: bool


class SkillListResponse(BaseModel):
    items: list[SkillResponse]


class SkillEffectiveResponse(BaseModel):
    slug: str
    name: str
    version: str
    manifest: dict[str, object]
    workspace_enabled: bool
    conversation_override: bool | None
    enabled: bool


class SkillEffectiveListResponse(BaseModel):
    items: list[SkillEffectiveResponse]


class SkillOverrideRequest(BaseModel):
    enabled: bool


class ToolApprovalResponse(BaseModel):
    id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    risk_level: str
    parameters_hash: str
    safe_summary: str
    status: str
    expires_at: datetime


class ToolApprovalListResponse(BaseModel):
    items: list[ToolApprovalResponse]


class ToolApprovalDecisionResponse(BaseModel):
    id: str
    run_id: str
    status: str


class ToolRunResponse(BaseModel):
    id: str
    tool_call_id: str
    tool_name: str
    status: str
    result_summary: str | None
    error_summary: str | None


class ToolRunListResponse(BaseModel):
    items: list[ToolRunResponse]


class McpToolResponse(BaseModel):
    name: str
    description: str
    input_schema: dict[str, object]


class McpToolListResponse(BaseModel):
    workspace_enabled: bool
    items: list[McpToolResponse]


def create_app(
    settings: Settings | None = None,
    *,
    model_gateway: ModelGateway | None = None,
    web_search_gateway: WebSearchGateway | None = None,
    web_page_gateway: WebAcquisitionGateway | None = None,
    graph_runner: ResearchGraphRunner | None = None,
    mcp_gateway: McpGateway | None = None,
    embedding_gateway: EmbeddingGateway | None = None,
    rerank_gateway: RerankGateway | None = None,
    embedded_worker: bool = False,
) -> FastAPI:
    resolved_settings = settings or Settings()
    engine = build_engine(resolved_settings.database_url)
    session_factory = build_session_factory(engine)
    resolved_model_gateway = model_gateway or build_model_gateway(
        api_key=resolved_settings.openai_api_key,
        api_base=resolved_settings.openai_api_base,
        model=resolved_settings.openai_model,
        reasoning_effort=resolved_settings.openai_reasoning_effort,
    )
    resolved_web_search_gateway = web_search_gateway or build_web_search_gateway(
        api_key=resolved_settings.brave_search_api_key
    )
    resolved_web_page_gateway = web_page_gateway or WebAcquisition(
            jina_reader=JinaReaderWebPageAdapter(
                api_key=resolved_settings.jina_reader_api_key
            ),
            local_reader=HttpWebPageGateway(),
    )
    resolved_embedding_gateway = embedding_gateway
    resolved_rerank_gateway = rerank_gateway
    retrieval_configured = any(
        bool(value)
        for value in (
            resolved_settings.embedding_api_key,
            resolved_settings.embedding_model,
            resolved_settings.rerank_api_key,
            resolved_settings.rerank_model,
        )
    )
    if resolved_embedding_gateway is None and retrieval_configured:
        if not resolved_settings.embedding_api_key or not resolved_settings.embedding_model:
            raise ValueError("embedding 配置不完整")
        resolved_embedding_gateway = LiteLLMEmbeddingGateway(
            api_key=resolved_settings.embedding_api_key,
            model=resolved_settings.embedding_model,
            api_base=resolved_settings.embedding_api_base,
        )
    if resolved_rerank_gateway is None and retrieval_configured:
        if not resolved_settings.rerank_api_key or not resolved_settings.rerank_model:
            raise ValueError("rerank 配置不完整")
        resolved_rerank_gateway = LiteLLMRerankGateway(
            api_key=resolved_settings.rerank_api_key,
            model=resolved_settings.rerank_model,
            api_base=resolved_settings.rerank_api_base,
        )
    token_estimator = LiteLLMTokenEstimator(resolved_settings.openai_model)
    retrieval = (
        HybridRetrieval(
            resolved_rerank_gateway,
            embedding_gateway=resolved_embedding_gateway,
            token_estimator=token_estimator,
            session_factory=session_factory,
            source_chunk_adapter=PostgresSourceChunkRetrievalAdapter(),
            conversation_segment_adapter=PostgresConversationSegmentRetrievalAdapter(),
            memory_adapter=PostgresMemoryRetrievalAdapter(),
            research_record_adapter=PostgresResearchRecordRetrievalAdapter(),
        )
        if resolved_rerank_gateway
        else None
    )
    resolved_mcp_gateway = mcp_gateway
    if resolved_mcp_gateway is None and resolved_settings.trusted_mcp_url:
        resolved_mcp_gateway = LocalTrustedHttpMcpAdapter(
            resolved_settings.trusted_mcp_url,
            timeout_seconds=resolved_settings.trusted_mcp_timeout_seconds,
        )
    tool_execution = ToolExecutionService(
        session_factory,
        resolved_mcp_gateway or DisabledMcpGateway(),
        approval_ttl_seconds=resolved_settings.tool_approval_ttl_seconds,
    )
    resolved_graph_runner = graph_runner or ResearchGraphRunner(
        resolved_settings.database_url,
        max_concurrency=resolved_settings.map_work_max_concurrency,
    )
    coordinator = ResearchCoordinator(
        session_factory,
        model_gateway=resolved_model_gateway,
        web_search_gateway=resolved_web_search_gateway,
        web_page_gateway=resolved_web_page_gateway,
        quota_service=QuotaService(
            token_limit=resolved_settings.workspace_token_quota,
            cost_limit_usd=resolved_settings.workspace_cost_quota_usd,
        ),
        tool_execution=tool_execution,
        run_token_budget=resolved_settings.run_token_budget,
        run_cost_budget_usd=resolved_settings.run_cost_budget_usd,
        model_context_tokens=resolved_settings.model_context_tokens,
        model_output_token_reserve=resolved_settings.model_output_token_reserve,
        model_context_safety_margin=resolved_settings.model_context_safety_margin,
        token_estimator=token_estimator,
        require_web_search_for_external_model=bool(
            getattr(resolved_model_gateway, "requires_web_research", False)
        ),
        step_delay_seconds=resolved_settings.research_step_delay_seconds,
        graph_runner=resolved_graph_runner,
        embedding_gateway=resolved_embedding_gateway,
        retrieval=retrieval,
        sandbox_submitter=lambda execution_id: sandbox_executor.submit(
            run_sandbox_execution, execution_id
        ),
        sandbox_canceller=lambda execution_id: sandbox.cancel(str(execution_id)),
        conversation_segment_submitter=lambda conversation_id: file_executor.submit(
            conversation_segment_processor.process, conversation_id
        ),
        research_record_submitter=lambda record_id: file_executor.submit(
            research_record_indexer.process, record_id
        ),
    )
    object_store = LocalObjectStore(resolved_settings.object_store_root)
    document_processor = DocumentProcessor(
        session_factory,
        object_store,
        embedding_gateway=resolved_embedding_gateway,
    )
    conversation_segment_processor = ConversationSegmentProcessor(
        session_factory,
        embedding_gateway=resolved_embedding_gateway,
    )
    memory_indexer = MemoryIndexer(
        session_factory,
        embedding_gateway=resolved_embedding_gateway,
    )
    research_record_indexer = ResearchRecordIndexer(
        session_factory,
        embedding_gateway=resolved_embedding_gateway,
    )
    sandbox = DockerSandbox(image=resolved_settings.sandbox_image)
    file_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="document-process")
    sandbox_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sandbox")
    run_queue = RunQueue(session_factory)
    run_worker = RunWorker(run_queue, coordinator)
    embedded_worker_enabled = embedded_worker

    def upgrade_schema() -> None:
        """使用 Alembic 将当前数据库升级到最新业务 schema"""
        repository_root = Path(__file__).resolve().parents[4]
        alembic_config = Config(str(repository_root / "alembic.ini"))
        alembic_config.set_main_option("sqlalchemy.url", resolved_settings.database_url)
        command.upgrade(alembic_config, "head")

    def ensure_builtin_skills() -> None:
        manifest = validate_manifest(SOURCE_COMPARISON_MANIFEST)
        with session_factory.begin() as session:
            package = session.scalar(
                select(SkillPackage).where(SkillPackage.slug == "source-comparison")
            )
            if package is None:
                package = SkillPackage(
                    slug="source-comparison",
                    name="来源比较",
                    description="比较同一主张的多份来源，保留一致、差异与证据不足。",
                    publisher="深度研究工作台",
                    trusted=True,
                )
                session.add(package)
                session.flush()
            version = session.scalar(
                select(SkillVersion).where(
                    SkillVersion.skill_package_id == package.id,
                    SkillVersion.version == "1.0.0",
                )
            )
            if version is None:
                session.add(
                    SkillVersion(
                        skill_package_id=package.id,
                        version="1.0.0",
                        content_hash=manifest_hash(manifest),
                        manifest=manifest,
                    )
                )

    async def initialize_runtime() -> None:
        """初始化 API 与独立 Worker 共用的数据库和运行时资源"""
        resolved_settings.object_store_root.mkdir(parents=True, exist_ok=True)
        resolved_settings.sandbox_output_root.mkdir(parents=True, exist_ok=True)
        upgrade_schema()
        if isinstance(resolved_graph_runner, ResearchGraphRunner):
            await resolved_graph_runner.setup_checkpointer()
        ensure_builtin_skills()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await initialize_runtime()
        app.state.session_factory = session_factory
        app.state.run_worker = run_worker
        app.state.run_queue = run_queue
        app.state.run_coordinator = coordinator
        app.state.tool_execution = tool_execution
        app.state.graph_runner = resolved_graph_runner
        if embedded_worker_enabled:
            run_worker.start()
        yield
        if embedded_worker_enabled:
            run_worker.stop()
        file_executor.shutdown(wait=True, cancel_futures=True)
        sandbox_executor.shutdown(wait=True, cancel_futures=True)
        engine.dispose()

    app = FastAPI(title="深度研究工作台", lifespan=lifespan)
    app.state.initialize_runtime = initialize_runtime
    app.state.session_factory = session_factory
    app.state.run_worker = run_worker
    app.state.run_queue = run_queue
    app.state.run_coordinator = coordinator
    app.state.tool_execution = tool_execution
    app.state.graph_runner = resolved_graph_runner

    def get_session() -> Iterator[Session]:
        yield from session_scope(session_factory)

    SessionDependency = Annotated[Session, Depends(get_session)]

    def require_user(
        session: SessionDependency,
        authorization: Annotated[str | None, Header()] = None,
    ) -> User:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
        token = authorization.removeprefix("Bearer ").strip()
        auth_session = session.scalar(
            select(AuthSession).where(AuthSession.token_hash == hash_access_token(token))
        )
        now = datetime.now(UTC)
        if auth_session is None or auth_session.expires_at.replace(tzinfo=UTC) <= now:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
        user = session.get(User, auth_session.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
        return user

    CurrentUser = Annotated[User, Depends(require_user)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/auth/register", response_model=AuthResponse, status_code=201)
    def register(payload: RegisterRequest, session: SessionDependency) -> AuthResponse:
        email = str(payload.email).strip().lower()
        if session.scalar(select(User.id).where(User.email == email)) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已注册")

        user = User(email=email, password_hash=hash_password(payload.password))
        session.add(user)
        session.flush()

        token = issue_access_token()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_access_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=resolved_settings.auth_session_days),
            )
        )
        return AuthResponse(access_token=token)

    @app.get("/api/v1/auth/me", response_model=CurrentUserResponse)
    def current_user(user: CurrentUser) -> CurrentUserResponse:
        return CurrentUserResponse(id=str(user.id), email=user.email)

    def workspace_response(workspace: Workspace) -> WorkspaceResponse:
        return WorkspaceResponse(
            id=str(workspace.id),
            name=workspace.name,
            description=workspace.description,
            instructions=workspace.instructions,
            archived=workspace.archived_at is not None,
            memory_auto_apply=workspace.memory_auto_apply,
        )

    def accessible_workspace(session: Session, user: User, workspace_id: UUID) -> Workspace:
        workspace = session.scalar(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                Workspace.id == workspace_id,
                WorkspaceMember.user_id == user.id,
                Workspace.deleted_at.is_(None),
            )
        )
        if workspace is None:
            raise HTTPException(status_code=404, detail="空间不存在")
        return workspace

    @app.post("/api/v1/workspaces", response_model=WorkspaceResponse, status_code=201)
    def create_workspace(
        payload: WorkspaceCreateRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> WorkspaceResponse:
        normalized_name = payload.name.strip()
        if not normalized_name:
            raise HTTPException(status_code=422, detail="空间名称不能为空")
        workspace = Workspace(
            name=normalized_name,
            description=payload.description,
            instructions=payload.instructions,
            memory_auto_apply=payload.memory_auto_apply,
        )
        session.add(workspace)
        session.flush()
        session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
        return workspace_response(workspace)

    @app.patch("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse)
    def update_workspace(
        workspace_id: UUID,
        payload: WorkspaceUpdateRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> WorkspaceResponse:
        workspace = accessible_workspace(session, user, workspace_id)
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="空间名称不能为空")
        workspace.name = name
        workspace.description = payload.description
        workspace.instructions = payload.instructions
        workspace.memory_auto_apply = payload.memory_auto_apply
        return workspace_response(workspace)

    @app.get("/api/v1/workspaces", response_model=WorkspaceListResponse)
    def list_workspaces(
        session: SessionDependency,
        user: CurrentUser,
        include_archived: bool = False,
    ) -> WorkspaceListResponse:
        statement = (
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                WorkspaceMember.user_id == user.id,
                Workspace.deleted_at.is_(None),
            )
            .order_by(Workspace.created_at)
        )
        if not include_archived:
            statement = statement.where(Workspace.archived_at.is_(None))
        workspaces = session.scalars(statement).all()
        return WorkspaceListResponse(items=[workspace_response(item) for item in workspaces])

    @app.post("/api/v1/workspaces/{workspace_id}/archive", response_model=WorkspaceResponse)
    def archive_workspace(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> WorkspaceResponse:
        workspace = accessible_workspace(session, user, workspace_id)
        workspace.archived_at = datetime.now(UTC)
        return workspace_response(workspace)

    @app.post("/api/v1/workspaces/{workspace_id}/restore", response_model=WorkspaceResponse)
    def restore_workspace(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> WorkspaceResponse:
        workspace = accessible_workspace(session, user, workspace_id)
        workspace.archived_at = None
        return workspace_response(workspace)

    @app.get(
        "/api/v1/workspaces/{workspace_id}/delete-preview",
        response_model=WorkspaceDeletePreviewResponse,
    )
    def preview_workspace_deletion(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> WorkspaceDeletePreviewResponse:
        accessible_workspace(session, user, workspace_id)
        conversations = (
            session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(
                    Conversation.workspace_id == workspace_id,
                    Conversation.deleted_at.is_(None),
                )
            )
            or 0
        )
        documents = (
            session.scalar(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.workspace_id == workspace_id,
                    Document.deleted_at.is_(None),
                )
            )
            or 0
        )
        memories = (
            session.scalar(
                select(func.count())
                .select_from(Memory)
                .where(
                    Memory.workspace_id == workspace_id,
                    Memory.deleted_at.is_(None),
                )
            )
            or 0
        )
        return WorkspaceDeletePreviewResponse(
            conversations=conversations,
            documents=documents,
            memories=memories,
            artifacts=0,
        )

    @app.delete("/api/v1/workspaces/{workspace_id}", status_code=204)
    def delete_workspace(
        workspace_id: UUID,
        payload: WorkspaceDeleteRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> Response:
        workspace = accessible_workspace(session, user, workspace_id)
        if payload.confirm_name != workspace.name:
            raise HTTPException(status_code=409, detail="确认名称与空间名称不一致")
        workspace.deleted_at = datetime.now(UTC)
        return Response(status_code=204)

    def conversation_response(conversation: Conversation) -> ConversationResponse:
        return ConversationResponse(
            id=str(conversation.id),
            workspace_id=str(conversation.workspace_id),
            title=conversation.title,
            archived=conversation.archived_at is not None,
        )

    @app.post(
        "/api/v1/workspaces/{workspace_id}/conversations",
        response_model=ConversationResponse,
        status_code=201,
    )
    def create_conversation(
        workspace_id: UUID,
        payload: ConversationCreateRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ConversationResponse:
        accessible_workspace(session, user, workspace_id)
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="会话标题不能为空")
        conversation = Conversation(workspace_id=workspace_id, title=title)
        session.add(conversation)
        session.flush()
        return conversation_response(conversation)

    @app.get(
        "/api/v1/workspaces/{workspace_id}/conversations",
        response_model=ConversationListResponse,
    )
    def list_conversations(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
        include_archived: bool = False,
    ) -> ConversationListResponse:
        accessible_workspace(session, user, workspace_id)
        statement = select(Conversation).where(
            Conversation.workspace_id == workspace_id,
            Conversation.deleted_at.is_(None),
        )
        if not include_archived:
            statement = statement.where(Conversation.archived_at.is_(None))
        conversations = session.scalars(statement.order_by(Conversation.updated_at.desc())).all()
        return ConversationListResponse(
            items=[conversation_response(item) for item in conversations]
        )

    def accessible_conversation(
        session: Session, user: User, conversation_id: UUID
    ) -> Conversation:
        conversation = session.scalar(
            select(Conversation)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Conversation.workspace_id)
            .join(Workspace, Workspace.id == Conversation.workspace_id)
            .where(
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
                Workspace.deleted_at.is_(None),
                WorkspaceMember.user_id == user.id,
            )
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return conversation

    def skill_package_by_slug(session: Session, slug: str) -> tuple[SkillPackage, SkillVersion]:
        package = session.scalar(select(SkillPackage).where(SkillPackage.slug == slug))
        if package is None or not package.trusted:
            raise HTTPException(status_code=404, detail="Skill 不存在")
        version = session.scalar(
            select(SkillVersion)
            .where(SkillVersion.skill_package_id == package.id)
            .order_by(SkillVersion.created_at.desc())
        )
        if version is None:
            raise HTTPException(status_code=404, detail="Skill 版本不存在")
        validate_manifest(version.manifest)
        return package, version

    def skill_response(
        package: SkillPackage, version: SkillVersion, *, installed: bool
    ) -> SkillResponse:
        return SkillResponse(
            slug=package.slug,
            name=package.name,
            description=package.description,
            publisher=package.publisher,
            version=version.version,
            content_hash=version.content_hash,
            manifest=version.manifest,
            installed=installed,
        )

    def accessible_installation(
        session: Session, user: User, slug: str
    ) -> tuple[SkillPackage, SkillVersion, SkillInstallation]:
        package, version = skill_package_by_slug(session, slug)
        installation = session.scalar(
            select(SkillInstallation).where(
                SkillInstallation.user_id == user.id,
                SkillInstallation.skill_package_id == package.id,
            )
        )
        if installation is None:
            raise HTTPException(status_code=409, detail="请先安装 Skill")
        return package, version, installation

    @app.get("/api/v1/skills/catalog", response_model=SkillListResponse)
    def list_skill_catalog(session: SessionDependency, user: CurrentUser) -> SkillListResponse:
        rows = session.execute(
            select(SkillPackage, SkillVersion)
            .join(SkillVersion, SkillVersion.skill_package_id == SkillPackage.id)
            .where(SkillPackage.trusted.is_(True))
            .order_by(SkillPackage.slug, SkillVersion.created_at.desc())
        ).all()
        latest: dict[str, tuple[SkillPackage, SkillVersion]] = {}
        for package, version in rows:
            latest.setdefault(package.slug, (package, version))
        installed_ids = set(
            session.scalars(
                select(SkillInstallation.skill_package_id).where(
                    SkillInstallation.user_id == user.id
                )
            ).all()
        )
        return SkillListResponse(
            items=[
                skill_response(package, version, installed=package.id in installed_ids)
                for package, version in latest.values()
            ]
        )

    @app.post("/api/v1/skills/{slug}/install", response_model=SkillResponse, status_code=201)
    def install_skill(slug: str, session: SessionDependency, user: CurrentUser) -> SkillResponse:
        package, version = skill_package_by_slug(session, slug)
        installation = session.scalar(
            select(SkillInstallation).where(
                SkillInstallation.user_id == user.id,
                SkillInstallation.skill_package_id == package.id,
            )
        )
        if installation is None:
            session.add(
                SkillInstallation(
                    user_id=user.id,
                    skill_package_id=package.id,
                    skill_version_id=version.id,
                )
            )
        return skill_response(package, version, installed=True)

    @app.post("/api/v1/workspaces/{workspace_id}/skills/{slug}/enable")
    def enable_workspace_skill(
        workspace_id: UUID,
        slug: str,
        session: SessionDependency,
        user: CurrentUser,
    ) -> dict[str, object]:
        accessible_workspace(session, user, workspace_id)
        package, _, installation = accessible_installation(session, user, slug)
        grant = session.scalar(
            select(WorkspaceSkillGrant).where(
                WorkspaceSkillGrant.workspace_id == workspace_id,
                WorkspaceSkillGrant.skill_installation_id == installation.id,
            )
        )
        if grant is None:
            grant = WorkspaceSkillGrant(
                workspace_id=workspace_id, skill_installation_id=installation.id, enabled=True
            )
            session.add(grant)
        else:
            grant.enabled = True
        return {"slug": package.slug, "enabled": True}

    @app.post("/api/v1/workspaces/{workspace_id}/skills/{slug}/disable")
    def disable_workspace_skill(
        workspace_id: UUID,
        slug: str,
        session: SessionDependency,
        user: CurrentUser,
    ) -> dict[str, object]:
        accessible_workspace(session, user, workspace_id)
        package, _, installation = accessible_installation(session, user, slug)
        grant = session.scalar(
            select(WorkspaceSkillGrant).where(
                WorkspaceSkillGrant.workspace_id == workspace_id,
                WorkspaceSkillGrant.skill_installation_id == installation.id,
            )
        )
        if grant is None:
            return {"slug": package.slug, "enabled": False}
        grant.enabled = False
        return {"slug": package.slug, "enabled": False}

    @app.post("/api/v1/workspaces/{workspace_id}/mcp/local-trusted/enable")
    def enable_workspace_mcp(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> dict[str, object]:
        """在指定 Workspace 启用本地受信 MCP"""
        accessible_workspace(session, user, workspace_id)
        try:
            enabled = tool_execution.set_workspace_enabled(workspace_id, True)
        except ToolApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"slug": "local-trusted", "enabled": enabled}

    @app.post("/api/v1/workspaces/{workspace_id}/mcp/local-trusted/disable")
    def disable_workspace_mcp(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> dict[str, object]:
        """在指定 Workspace 停用本地受信 MCP 并失效待审批调用"""
        accessible_workspace(session, user, workspace_id)
        enabled = tool_execution.set_workspace_enabled(workspace_id, False)
        return {"slug": "local-trusted", "enabled": enabled}

    @app.get(
        "/api/v1/conversations/{conversation_id}/skills",
        response_model=SkillEffectiveListResponse,
    )
    def list_effective_skills(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> SkillEffectiveListResponse:
        conversation = accessible_conversation(session, user, conversation_id)
        grants = session.scalars(
            select(WorkspaceSkillGrant)
            .join(
                SkillInstallation,
                SkillInstallation.id == WorkspaceSkillGrant.skill_installation_id,
            )
            .where(
                WorkspaceSkillGrant.workspace_id == conversation.workspace_id,
                SkillInstallation.user_id == user.id,
            )
        ).all()
        items: list[SkillEffectiveResponse] = []
        for grant in grants:
            installation = session.get(SkillInstallation, grant.skill_installation_id)
            if installation is None:
                continue
            package = session.get(SkillPackage, installation.skill_package_id)
            version = session.get(SkillVersion, installation.skill_version_id)
            if package is None or version is None:
                continue
            override = session.scalar(
                select(ConversationSkillOverride).where(
                    ConversationSkillOverride.conversation_id == conversation.id,
                    ConversationSkillOverride.skill_installation_id == installation.id,
                )
            )
            items.append(
                SkillEffectiveResponse(
                    slug=package.slug,
                    name=package.name,
                    version=version.version,
                    manifest=version.manifest,
                    workspace_enabled=grant.enabled,
                    conversation_override=(override.enabled if override is not None else None),
                    enabled=grant.enabled and (override.enabled if override is not None else True),
                )
            )
        return SkillEffectiveListResponse(items=items)

    def skill_tool_policy(
        session: Session, conversation: Conversation, user: User
    ) -> frozenset[str] | None:
        """读取当前会话的 Skill 工具约束，区分无约束与空交集"""
        rows = session.execute(
            select(
                SkillVersion.manifest,
                WorkspaceSkillGrant.enabled,
                ConversationSkillOverride.enabled,
            )
            .join(
                SkillInstallation,
                SkillInstallation.skill_version_id == SkillVersion.id,
            )
            .join(
                WorkspaceSkillGrant,
                WorkspaceSkillGrant.skill_installation_id == SkillInstallation.id,
            )
            .outerjoin(
                ConversationSkillOverride,
                (
                    ConversationSkillOverride.conversation_id == conversation.id
                )
                & (
                    ConversationSkillOverride.skill_installation_id
                    == SkillInstallation.id
                ),
            )
            .where(
                SkillInstallation.user_id == user.id,
                WorkspaceSkillGrant.workspace_id == conversation.workspace_id,
            )
        ).all()
        if not rows:
            return None
        allowed_tools: set[str] = set()
        for manifest, workspace_enabled, conversation_enabled in rows:
            enabled = workspace_enabled and (
                conversation_enabled if conversation_enabled is not None else True
            )
            if not enabled:
                continue
            manifest_tools = manifest.get("allowed_tools", [])
            if isinstance(manifest_tools, list):
                allowed_tools.update(tool for tool in manifest_tools if isinstance(tool, str))
        return frozenset(allowed_tools)

    @app.get(
        "/api/v1/conversations/{conversation_id}/mcp/local-trusted/tools",
        response_model=McpToolListResponse,
    )
    async def list_effective_mcp_tools(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> McpToolListResponse:
        """返回当前会话实际允许使用的本地受信 MCP 工具"""
        conversation = accessible_conversation(session, user, conversation_id)
        workspace_enabled = bool(
            session.scalar(
                select(WorkspaceMcpGrant.enabled).where(
                    WorkspaceMcpGrant.workspace_id == conversation.workspace_id,
                    WorkspaceMcpGrant.plugin_slug == "local-trusted",
                )
            )
        )
        tools = await tool_execution.list_effective_tools(
            conversation.workspace_id,
            agent_role="researcher",
            skill_allowed_tools=skill_tool_policy(session, conversation, user),
        )
        return McpToolListResponse(
            workspace_enabled=workspace_enabled,
            items=[McpToolResponse(**tool) for tool in tools],
        )

    @app.put(
        "/api/v1/conversations/{conversation_id}/skills/{slug}/override",
        response_model=dict[str, object],
    )
    def override_conversation_skill(
        conversation_id: UUID,
        slug: str,
        payload: SkillOverrideRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> dict[str, object]:
        conversation = accessible_conversation(session, user, conversation_id)
        package, _, installation = accessible_installation(session, user, slug)
        grant = session.scalar(
            select(WorkspaceSkillGrant).where(
                WorkspaceSkillGrant.workspace_id == conversation.workspace_id,
                WorkspaceSkillGrant.skill_installation_id == installation.id,
            )
        )
        if grant is None:
            raise HTTPException(status_code=409, detail="请先在空间启用 Skill")
        override = session.scalar(
            select(ConversationSkillOverride).where(
                ConversationSkillOverride.conversation_id == conversation.id,
                ConversationSkillOverride.skill_installation_id == installation.id,
            )
        )
        if override is None:
            override = ConversationSkillOverride(
                workspace_id=conversation.workspace_id,
                conversation_id=conversation.id,
                skill_installation_id=installation.id,
                enabled=payload.enabled,
            )
            session.add(override)
        else:
            override.enabled = payload.enabled
        return {"slug": package.slug, "enabled": payload.enabled}

    @app.patch("/api/v1/conversations/{conversation_id}", response_model=ConversationResponse)
    def update_conversation(
        conversation_id: UUID,
        payload: ConversationUpdateRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ConversationResponse:
        conversation = accessible_conversation(session, user, conversation_id)
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="会话标题不能为空")
        conversation.title = title
        conversation.updated_at = datetime.now(UTC)
        return conversation_response(conversation)

    @app.post(
        "/api/v1/conversations/{conversation_id}/archive",
        response_model=ConversationResponse,
    )
    def archive_conversation(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ConversationResponse:
        conversation = accessible_conversation(session, user, conversation_id)
        conversation.archived_at = datetime.now(UTC)
        return conversation_response(conversation)

    @app.post(
        "/api/v1/conversations/{conversation_id}/restore",
        response_model=ConversationResponse,
    )
    def restore_conversation(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ConversationResponse:
        conversation = accessible_conversation(session, user, conversation_id)
        conversation.archived_at = None
        return conversation_response(conversation)

    def add_memory_revision(
        session: Session,
        memory: Memory,
        *,
        status_value: str,
        reason: str,
    ) -> None:
        session.add(
            MemoryRevision(
                workspace_id=memory.workspace_id,
                memory_id=memory.id,
                content=memory.content,
                status=status_value,
                change_reason=reason,
            )
        )

    def expire_due_memories(session: Session, workspace_id: UUID) -> None:
        now = datetime.now(UTC)
        due_memories = session.scalars(
            select(Memory).where(
                Memory.workspace_id == workspace_id,
                Memory.status == "active",
                Memory.deleted_at.is_(None),
                Memory.expires_at.is_not(None),
                Memory.expires_at <= now,
            )
        ).all()
        for memory in due_memories:
            memory.status = "expired"
            memory.updated_at = now
            add_memory_revision(session, memory, status_value="expired", reason="expired")

    def create_governed_memory(
        session: Session,
        *,
        workspace: Workspace,
        user: User,
        content: str,
        scope: str,
        category: str,
        risk_level: str,
        conversation_id: UUID | None = None,
        source_message_id: UUID | None = None,
        expires_at: datetime | None = None,
        auto_activate: bool = False,
    ) -> Memory:
        normalized_content = content.strip()
        existing = session.scalar(
            select(Memory)
            .where(
                Memory.workspace_id == workspace.id,
                Memory.scope == scope,
                Memory.category == category,
                Memory.conversation_id == conversation_id,
                Memory.status == "active",
                Memory.deleted_at.is_(None),
            )
            .order_by(Memory.updated_at.desc())
        )
        desired_status = (
            "active"
            if auto_activate and risk_level == "low" and workspace.memory_auto_apply
            else "candidate"
        )
        status_value = (
            "conflicted"
            if existing is not None and existing.content != normalized_content
            else desired_status
        )
        memory = Memory(
            workspace_id=workspace.id,
            user_id=user.id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            scope=scope,
            category=category,
            risk_level=risk_level,
            content=normalized_content,
            status=status_value,
            expires_at=expires_at,
        )
        session.add(memory)
        session.flush()
        if status_value == "conflicted" and existing is not None:
            session.add(
                MemoryConflict(
                    workspace_id=workspace.id,
                    old_memory_id=existing.id,
                    new_memory_id=memory.id,
                    status="pending",
                )
            )
            reason = "conflict_detected"
        elif status_value == "active":
            reason = "auto_activated"
        else:
            reason = "candidate_created"
        add_memory_revision(session, memory, status_value=status_value, reason=reason)
        return memory

    def extract_explicit_memory(
        session: Session,
        *,
        workspace: Workspace,
        conversation: Conversation,
        user: User,
        source_message: Message,
    ) -> Memory | None:
        match = re.search(r"(?:请)?记住\s*[:：]\s*(.+)$", source_message.content, re.DOTALL)
        if match is None:
            return None
        content = match.group(1).strip()
        if not content or len(content) > 4000:
            return None
        secret_pattern = re.compile(
            r"(?i)(?:password|passwd|api[ _-]?key|access[ _-]?token|secret|bearer\s+|"
            r"sk-[a-z0-9_-]+|密码|口令|令牌|密钥)"
        )
        if secret_pattern.search(content):
            return None
        sensitive_pattern = re.compile(
            r"(?:身份证|护照|住址|病史|健康|诊断|收入|工资|财务|银行卡|公司机密|商业秘密)"
        )
        risk_level = "sensitive" if sensitive_pattern.search(content) else "low"
        if re.search(r"(?:口径|范围|必须|仅限|只看)", content):
            category = "constraint"
        elif re.search(r"(?:偏好|喜欢|默认|使用|语言|格式)", content):
            category = "preference"
        else:
            category = "fact"
        return create_governed_memory(
            session,
            workspace=workspace,
            user=user,
            content=content,
            scope="workspace",
            category=category,
            risk_level=risk_level,
            conversation_id=None,
            source_message_id=source_message.id,
            auto_activate=True,
        )

    @app.delete("/api/v1/conversations/{conversation_id}", status_code=204)
    def delete_conversation(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> Response:
        conversation = accessible_conversation(session, user, conversation_id)
        deleted_at = datetime.now(UTC)
        conversation.deleted_at = deleted_at
        indexed_segments = session.scalars(
            select(ConversationSegment).where(
                ConversationSegment.conversation_id == conversation.id,
                ConversationSegment.deleted_at.is_(None),
            )
        ).all()
        for segment in indexed_segments:
            segment.deleted_at = deleted_at
        sourced_candidates = session.scalars(
            select(Memory)
            .join(Message, Message.id == Memory.source_message_id)
            .where(
                Message.conversation_id == conversation.id,
                Memory.status.in_({"candidate", "conflicted"}),
                Memory.deleted_at.is_(None),
            )
        ).all()
        for memory in sourced_candidates:
            memory.status = "inactive"
            add_memory_revision(
                session,
                memory,
                status_value="inactive",
                reason="source_conversation_deleted",
            )
        return Response(status_code=204)

    def message_response(message: Message) -> MessageResponse:
        return MessageResponse(
            id=str(message.id),
            role=message.role,
            content=message.content,
            version=message.version,
        )

    def suggested_conversation_title(content: str) -> str:
        """根据首条研究问题生成简短会话标题"""
        compact = re.sub(r"\s+", " ", content.strip())
        first_sentence = re.split(r"[。！？!?；;]", compact, maxsplit=1)[0].strip()
        title = first_sentence or compact
        return f"{title[:38]}…" if len(title) > 38 else title

    @app.post(
        "/api/v1/conversations/{conversation_id}/messages",
        response_model=RunCreatedResponse,
        status_code=202,
    )
    def send_message(
        conversation_id: UUID,
        payload: MessageCreateRequest,
        session: SessionDependency,
        user: CurrentUser,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RunCreatedResponse:
        conversation = accessible_conversation(session, user, conversation_id)
        existing = session.scalar(
            select(ResearchRun).where(
                ResearchRun.conversation_id == conversation_id,
                ResearchRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return RunCreatedResponse(
                run_id=str(existing.id),
                assistant_message_id=str(existing.assistant_message_id),
                status=existing.status,
            )

        content = payload.content.strip()
        if not content:
            raise HTTPException(status_code=422, detail="消息内容不能为空")
        if conversation.title in {"新会话", "研究会话"}:
            conversation.title = suggested_conversation_title(content)
        unique_attachment_ids = set(payload.attachment_ids)
        attachments = (
            session.scalars(
                select(Attachment).where(
                    Attachment.id.in_(unique_attachment_ids),
                    Attachment.workspace_id == conversation.workspace_id,
                    Attachment.conversation_id == conversation.id,
                    Attachment.deleted_at.is_(None),
                )
            ).all()
            if unique_attachment_ids
            else []
        )
        if len(attachments) != len(unique_attachment_ids):
            raise HTTPException(status_code=404, detail="附件不存在于当前会话")
        user_message = Message(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            role="user",
            content=content,
        )
        assistant_message = Message(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            role="assistant",
            content="",
        )
        session.add_all([user_message, assistant_message])
        session.flush()
        session.add_all(
            MessageAttachment(
                workspace_id=conversation.workspace_id,
                conversation_id=conversation.id,
                message_id=user_message.id,
                attachment_id=attachment.id,
            )
            for attachment in attachments
        )
        run = ResearchRun(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            initiated_by_user_id=user.id,
            trigger_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            idempotency_key=idempotency_key,
            status="queued",
            next_event_seq=2,
        )
        session.add(run)
        session.flush()
        session.add(
            ResearchLedger(
                workspace_id=conversation.workspace_id,
                run_id=run.id,
                goal=content,
                status="running",
            )
        )
        session.add(
            RunEvent(
                workspace_id=conversation.workspace_id,
                run_id=run.id,
                seq=1,
                type="run_queued",
                payload={"message": "研究已进入队列"},
            )
        )
        workspace = session.get(Workspace, conversation.workspace_id)
        if workspace is not None:
            extract_explicit_memory(
                session,
                workspace=workspace,
                conversation=conversation,
                user=user,
                source_message=user_message,
            )
        conversation.updated_at = datetime.now(UTC)
        session.commit()
        file_executor.submit(conversation_segment_processor.process, conversation.id)
        file_executor.submit(memory_indexer.process_workspace, conversation.workspace_id)
        return RunCreatedResponse(
            run_id=str(run.id),
            assistant_message_id=str(assistant_message.id),
            status=run.status,
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/messages",
        response_model=MessageListResponse,
    )
    def list_messages(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> MessageListResponse:
        accessible_conversation(session, user, conversation_id)
        messages = session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.deleted_at.is_(None),
            )
            .order_by(Message.created_at, Message.role.desc())
        ).all()
        return MessageListResponse(items=[message_response(item) for item in messages])

    @app.get(
        "/api/v1/conversations/{conversation_id}/active-run",
        response_model=RunCreatedResponse | None,
    )
    def get_active_run(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> RunCreatedResponse | None:
        accessible_conversation(session, user, conversation_id)
        run = session.scalar(
            select(ResearchRun)
            .where(
                ResearchRun.conversation_id == conversation_id,
                ResearchRun.status.in_(
                    {"queued", "running", "cancel_requested", "waiting_approval"}
                ),
            )
            .order_by(ResearchRun.created_at.desc())
        )
        if run is None:
            return None
        return RunCreatedResponse(
            run_id=str(run.id),
            assistant_message_id=str(run.assistant_message_id),
            status=run.status,
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/latest-run",
        response_model=RunDetailResponse | None,
    )
    def get_latest_run(
        conversation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> RunDetailResponse | None:
        accessible_conversation(session, user, conversation_id)
        run = session.scalar(
            select(ResearchRun)
            .where(ResearchRun.conversation_id == conversation_id)
            .order_by(ResearchRun.created_at.desc())
        )
        if run is None:
            return None
        tasks = session.scalars(
            select(ResearchTask).where(ResearchTask.run_id == run.id).order_by(ResearchTask.ordinal)
        ).all()
        usage = session.scalar(select(UsageLedger).where(UsageLedger.run_id == run.id))
        return RunDetailResponse(
            run_id=str(run.id),
            status=run.status,
            tasks=[
                ResearchTaskResponse(
                    ordinal=task.ordinal,
                    title=task.title,
                    status=task.status,
                    failure_impact=task.failure_impact,
                    role=task.role,
                    depth=task.depth,
                    token_budget=task.token_budget,
                    time_budget_seconds=task.time_budget_seconds,
                    allowed_tools=task.allowed_tools,
                )
                for task in tasks
            ],
            usage=(
                ModelUsageResponse(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    cost_usd=usage.cost_micros / 1_000_000,
                )
                if usage is not None
                else None
            ),
        )

    @app.get("/api/v1/runs/{run_id}/ledger", response_model=ResearchLedgerResponse)
    def get_research_ledger(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ResearchLedgerResponse:
        """回读当前用户有权限的 Research Ledger 生命周期事实"""
        run = accessible_run(session, user, run_id)
        ledger = session.scalar(select(ResearchLedger).where(ResearchLedger.run_id == run.id))
        if ledger is None:
            raise HTTPException(status_code=404, detail="研究账本不存在")
        coverage = session.scalar(
            select(CoverageSnapshot)
            .where(CoverageSnapshot.ledger_id == ledger.id)
            .order_by(CoverageSnapshot.created_at.desc())
        )
        stop_decision = session.scalar(
            select(StopDecision).where(StopDecision.ledger_id == ledger.id)
        )
        gaps = session.scalars(
            select(EvidenceGap)
            .where(EvidenceGap.ledger_id == ledger.id)
            .order_by(EvidenceGap.created_at)
        ).all()
        map_works = session.scalars(
            select(SourceMapWork).where(
                SourceMapWork.run_id == run.id,
                SourceMapWork.workspace_id == run.workspace_id,
            )
        ).all()
        return ResearchLedgerResponse(
            id=str(ledger.id),
            run_id=str(ledger.run_id),
            goal=ledger.goal,
            status=ledger.status,
            coverage=(
                LedgerCoverageResponse(
                    citation_count=coverage.citation_count,
                    verified_claim_count=coverage.verified_claim_count,
                    complete=coverage.complete,
                )
                if coverage is not None
                else None
            ),
            gaps=[
                LedgerGapResponse(
                    id=str(gap.id), description=gap.description, status=gap.status
                )
                for gap in gaps
            ],
            stop_decision=(
                LedgerStopDecisionResponse(
                    reason=stop_decision.reason,
                    completeness=stop_decision.completeness,
                )
                if stop_decision is not None
                else None
            ),
            missing_chunk_ids=list(missing_source_map_chunk_ids(map_works)),
        )

    @app.get(
        "/api/v1/runs/{run_id}/map-works",
        response_model=SourceMapWorkListResponse,
    )
    def list_source_map_works(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> SourceMapWorkListResponse:
        """返回当前用户可访问运行的 bounded map work 账本"""
        run = accessible_run(session, user, run_id)
        works = session.scalars(
            select(SourceMapWork)
            .where(
                SourceMapWork.run_id == run.id,
                SourceMapWork.workspace_id == run.workspace_id,
            )
            .order_by(SourceMapWork.created_at, SourceMapWork.id)
        ).all()
        return SourceMapWorkListResponse(
            items=[
                SourceMapWorkResponse(
                    id=str(work.id),
                    source_snapshot_id=str(work.source_snapshot_id),
                    snapshot_hash=work.snapshot_hash,
                    chunk_ids=work.chunk_ids,
                    chunk_hashes=work.chunk_hashes,
                    input_hash=work.input_hash,
                    prompt_version=work.prompt_version,
                    status=work.status,
                    digest=work.digest,
                    failure_reason=work.failure_reason,
                )
                for work in works
            ]
        )

    @app.get("/api/v1/runs/{run_id}/todos", response_model=TodoListResponse)
    def list_run_todos(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> TodoListResponse:
        """返回当前用户可访问运行的持久化 Todo 状态"""
        run = accessible_run(session, user, run_id)
        todos = session.scalars(
            select(Todo).where(Todo.run_id == run.id).order_by(Todo.ordinal, Todo.created_at)
        ).all()

        def todo_response(todo: Todo) -> TodoResponse:
            """将 Todo 和关联 Sandbox 转成用户可见摘要"""
            execution = (
                session.get(SandboxExecution, todo.sandbox_execution_id)
                if todo.sandbox_execution_id is not None
                else None
            )
            artifacts = (
                session.scalars(
                    select(Artifact).where(Artifact.sandbox_execution_id == execution.id)
                ).all()
                if execution is not None
                else []
            )
            return TodoResponse(
                id=str(todo.id),
                ordinal=todo.ordinal,
                title=todo.title,
                purpose=todo.purpose,
                kind=todo.kind,
                status=todo.status,
                result_summary=todo.result_summary,
                failure_reason=todo.failure_reason,
                sandbox_execution_id=str(execution.id) if execution is not None else None,
                sandbox_code=execution.code if execution is not None else None,
                sandbox_input_attachment_ids=(
                    execution.input_attachment_ids if execution is not None else []
                ),
                sandbox_artifacts=[
                    {
                        "id": str(artifact.id),
                        "filename": artifact.filename,
                        "media_type": artifact.media_type,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                    }
                    for artifact in artifacts
                ],
            )

        return TodoListResponse(
            items=[todo_response(todo) for todo in todos]
        )

    def attachment_response(attachment: Attachment) -> AttachmentResponse:
        return AttachmentResponse(
            id=str(attachment.id),
            conversation_id=str(attachment.conversation_id),
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
            status=attachment.status,
            failure_reason=attachment.failure_reason,
        )

    def accessible_attachment(session: Session, user: User, attachment_id: UUID) -> Attachment:
        attachment = session.scalar(
            select(Attachment)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Attachment.workspace_id)
            .join(Workspace, Workspace.id == Attachment.workspace_id)
            .where(
                Attachment.id == attachment_id,
                Attachment.deleted_at.is_(None),
                Workspace.deleted_at.is_(None),
                WorkspaceMember.user_id == user.id,
            )
        )
        if attachment is None:
            raise HTTPException(status_code=404, detail="附件不存在")
        return attachment

    @app.post(
        "/api/v1/conversations/{conversation_id}/attachments",
        response_model=AttachmentResponse,
        status_code=202,
    )
    def upload_attachment(
        conversation_id: UUID,
        file: UploadFile,
        session: SessionDependency,
        user: CurrentUser,
    ) -> AttachmentResponse:
        conversation = accessible_conversation(session, user, conversation_id)
        filename = Path(file.filename or "attachment").name
        if filename in {"", ".", ".."}:
            raise HTTPException(status_code=422, detail="附件名称无效")
        attachment = Attachment(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            filename=filename,
            storage_key="pending",
            mime_type=file.content_type or "application/octet-stream",
            size_bytes=0,
            sha256="",
            status="processing",
        )
        session.add(attachment)
        session.flush()
        try:
            storage_key, size_bytes, sha256 = object_store.put_attachment(
                conversation.workspace_id,
                attachment.id,
                file.file,
                max_bytes=resolved_settings.max_attachment_bytes,
            )
        except FileTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        attachment.storage_key = storage_key
        attachment.size_bytes = size_bytes
        attachment.sha256 = sha256
        session.commit()
        file_executor.submit(document_processor.process, attachment.id)
        return attachment_response(attachment)

    @app.get("/api/v1/attachments/{attachment_id}", response_model=AttachmentResponse)
    def get_attachment(
        attachment_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> AttachmentResponse:
        return attachment_response(accessible_attachment(session, user, attachment_id))

    def document_response(
        document: Document, document_version: DocumentVersion
    ) -> DocumentResponse:
        return DocumentResponse(
            id=str(document.id),
            filename=document.filename,
            version=document_version.version,
            sha256=document_version.sha256,
            mime_type=document_version.mime_type,
        )

    @app.post(
        "/api/v1/attachments/{attachment_id}/promote",
        response_model=DocumentResponse,
        status_code=201,
    )
    def promote_attachment(
        attachment_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> DocumentResponse:
        attachment = accessible_attachment(session, user, attachment_id)
        if attachment.status != "ready":
            raise HTTPException(status_code=409, detail="只有处理完成的附件可以加入空间")
        if attachment.promoted_document_id is not None:
            existing_document = session.get(Document, attachment.promoted_document_id)
            existing_version = (
                session.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == attachment.promoted_document_id,
                        DocumentVersion.version == existing_document.current_version,
                    )
                )
                if existing_document is not None
                else None
            )
            if existing_document is not None and existing_version is not None:
                return document_response(existing_document, existing_version)

        document = Document(
            workspace_id=attachment.workspace_id,
            filename=attachment.filename,
            current_version=1,
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            workspace_id=attachment.workspace_id,
            document_id=document.id,
            source_attachment_id=attachment.id,
            version=1,
            storage_key=attachment.storage_key,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
        )
        session.add(version)
        session.flush()
        private_chunks = session.scalars(
            select(SourceChunk)
            .where(SourceChunk.attachment_id == attachment.id)
            .order_by(SourceChunk.ordinal)
        ).all()
        for chunk in private_chunks:
            session.add(
                SourceChunk(
                    workspace_id=attachment.workspace_id,
                    conversation_id=None,
                    attachment_id=None,
                    document_version_id=version.id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    page_number=chunk.page_number,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    content_hash=chunk.content_hash,
                    embedding=list(chunk.embedding) if chunk.embedding is not None else None,
                    embedding_model=chunk.embedding_model,
                    embedding_dimensions=chunk.embedding_dimensions,
                    embedding_status=chunk.embedding_status,
                    embedding_error=chunk.embedding_error,
                    indexed_at=chunk.indexed_at,
                )
            )
        attachment.promoted_document_id = document.id
        return document_response(document, version)

    @app.get(
        "/api/v1/workspaces/{workspace_id}/research-records",
        response_model=ResearchRecordListResponse,
    )
    def list_research_records(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ResearchRecordListResponse:
        """列出当前 Workspace 已核验的不可变 Research Record"""
        accessible_workspace(session, user, workspace_id)
        records = session.scalars(
            select(ResearchRecord)
            .where(
                ResearchRecord.workspace_id == workspace_id,
                ResearchRecord.deleted_at.is_(None),
            )
            .order_by(ResearchRecord.created_at.desc())
        ).all()
        items: list[ResearchRecordResponse] = []
        for record in records:
            spans = session.scalars(
                select(EvidenceSpan)
                .join(
                    ResearchClaimEvidence,
                    ResearchClaimEvidence.evidence_span_id == EvidenceSpan.id,
                )
                .where(
                    ResearchClaimEvidence.claim_id == record.claim_id,
                    ResearchClaimEvidence.workspace_id == workspace_id,
                    ResearchClaimEvidence.relation.in_({"supports", "contradicts"}),
                )
                .order_by(EvidenceSpan.start_offset)
            ).all()
            items.append(
                ResearchRecordResponse(
                    id=str(record.id),
                    record_key=record.record_key,
                    version=record.version,
                    claim_text=record.claim_text,
                    status=record.status,
                    embedding_model=record.embedding_model,
                    embedding_status=record.embedding_status,
                    embedding_error=record.embedding_error,
                    evidence=[
                        ResearchRecordEvidenceResponse(
                            id=str(span.id),
                            source_chunk_id=str(span.source_chunk_id),
                            start_offset=span.start_offset,
                            end_offset=span.end_offset,
                            source_hash=span.content_hash,
                        )
                        for span in spans
                    ],
                )
            )
        return ResearchRecordListResponse(items=items)

    @app.get(
        "/api/v1/workspaces/{workspace_id}/documents",
        response_model=DocumentListResponse,
    )
    def list_documents(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> DocumentListResponse:
        accessible_workspace(session, user, workspace_id)
        rows = session.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                (DocumentVersion.document_id == Document.id)
                & (DocumentVersion.version == Document.current_version),
            )
            .where(Document.workspace_id == workspace_id, Document.deleted_at.is_(None))
            .order_by(Document.created_at)
        ).all()
        return DocumentListResponse(
            items=[document_response(document, version) for document, version in rows]
        )

    def accessible_document(session: Session, user: User, document_id: UUID) -> Document:
        document = session.scalar(
            select(Document)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Document.workspace_id)
            .join(Workspace, Workspace.id == Document.workspace_id)
            .where(
                Document.id == document_id,
                Document.deleted_at.is_(None),
                Workspace.deleted_at.is_(None),
                WorkspaceMember.user_id == user.id,
            )
        )
        if document is None:
            raise HTTPException(status_code=404, detail="空间文档不存在")
        return document

    @app.post(
        "/api/v1/documents/{document_id}/versions",
        response_model=DocumentResponse,
        status_code=201,
    )
    def replace_document(
        document_id: UUID,
        file: UploadFile,
        session: SessionDependency,
        user: CurrentUser,
    ) -> DocumentResponse:
        document = accessible_document(session, user, document_id)
        current_version = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == document.current_version,
            )
        )
        if current_version is None:
            raise HTTPException(status_code=409, detail="空间文档当前版本不存在")
        source_attachment = session.get(Attachment, current_version.source_attachment_id)
        if source_attachment is None:
            raise HTTPException(status_code=409, detail="空间文档来源附件不存在")
        filename = Path(file.filename or document.filename).name
        attachment = Attachment(
            workspace_id=document.workspace_id,
            conversation_id=source_attachment.conversation_id,
            filename=filename,
            storage_key="pending",
            mime_type=file.content_type or "application/octet-stream",
            size_bytes=0,
            sha256="",
            status="processing",
        )
        session.add(attachment)
        session.flush()
        try:
            storage_key, size_bytes, sha256 = object_store.put_attachment(
                document.workspace_id,
                attachment.id,
                file.file,
                max_bytes=resolved_settings.max_attachment_bytes,
            )
        except FileTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        attachment.storage_key = storage_key
        attachment.size_bytes = size_bytes
        attachment.sha256 = sha256
        session.commit()

        document_processor.process(attachment.id)
        session.expire_all()
        reloaded_attachment = session.get(Attachment, attachment.id)
        document = accessible_document(session, user, document_id)
        if reloaded_attachment is None or reloaded_attachment.status != "ready":
            detail = (
                reloaded_attachment.failure_reason
                if reloaded_attachment is not None
                else "附件不存在"
            )
            raise HTTPException(status_code=422, detail=detail or "新版本解析失败")

        attachment = reloaded_attachment

        next_version_number = document.current_version + 1
        version = DocumentVersion(
            workspace_id=document.workspace_id,
            document_id=document.id,
            source_attachment_id=attachment.id,
            version=next_version_number,
            storage_key=attachment.storage_key,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
        )
        session.add(version)
        session.flush()
        parsed_chunks = session.scalars(
            select(SourceChunk)
            .where(SourceChunk.attachment_id == attachment.id)
            .order_by(SourceChunk.ordinal)
        ).all()
        for chunk in parsed_chunks:
            session.add(
                SourceChunk(
                    workspace_id=document.workspace_id,
                    conversation_id=None,
                    attachment_id=None,
                    document_version_id=version.id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    page_number=chunk.page_number,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    content_hash=chunk.content_hash,
                )
            )
        attachment.promoted_document_id = document.id
        document.current_version = next_version_number
        document.filename = filename
        return document_response(document, version)

    def accessible_message(session: Session, user: User, message_id: UUID) -> Message:
        message = session.scalar(
            select(Message)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Message.workspace_id)
            .join(Workspace, Workspace.id == Message.workspace_id)
            .where(
                Message.id == message_id,
                Message.deleted_at.is_(None),
                Workspace.deleted_at.is_(None),
                WorkspaceMember.user_id == user.id,
            )
        )
        if message is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        return message

    def memory_conflict_response(session: Session, memory: Memory) -> MemoryConflictResponse | None:
        conflict = session.scalar(
            select(MemoryConflict).where(MemoryConflict.new_memory_id == memory.id)
        )
        if conflict is None:
            return None
        old_memory = session.get(Memory, conflict.old_memory_id)
        if old_memory is None:
            return None
        return MemoryConflictResponse(
            id=str(conflict.id),
            old_memory_id=str(old_memory.id),
            old_content=old_memory.content,
            new_memory_id=str(memory.id),
            new_content=memory.content,
            status=conflict.status,
            resolution=conflict.resolution,
        )

    def memory_response(session: Session, memory: Memory) -> MemoryResponse:
        return MemoryResponse(
            id=str(memory.id),
            workspace_id=str(memory.workspace_id),
            conversation_id=(str(memory.conversation_id) if memory.conversation_id else None),
            scope=memory.scope,
            category=memory.category,
            risk_level=memory.risk_level,
            content=memory.content,
            status=memory.status,
            expires_at=memory.expires_at,
            source_message_id=(str(memory.source_message_id) if memory.source_message_id else None),
            conflict=memory_conflict_response(session, memory),
        )

    def accessible_memory(session: Session, user: User, memory_id: UUID) -> Memory:
        memory = session.scalar(
            select(Memory)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Memory.workspace_id)
            .join(Workspace, Workspace.id == Memory.workspace_id)
            .where(
                Memory.id == memory_id,
                Memory.deleted_at.is_(None),
                Workspace.deleted_at.is_(None),
                WorkspaceMember.user_id == user.id,
            )
        )
        if memory is None:
            raise HTTPException(status_code=404, detail="长期记忆不存在")
        return memory

    @app.post(
        "/api/v1/workspaces/{workspace_id}/memories",
        response_model=MemoryResponse,
        status_code=201,
    )
    def create_memory(
        workspace_id: UUID,
        payload: MemoryCreateRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> MemoryResponse:
        workspace = accessible_workspace(session, user, workspace_id)
        if payload.scope not in {"user", "workspace", "conversation"}:
            raise HTTPException(status_code=422, detail="记忆范围无效")
        if payload.risk_level not in {"low", "sensitive", "high_impact"}:
            raise HTTPException(status_code=422, detail="记忆风险等级无效")
        if payload.scope == "conversation":
            if payload.conversation_id is None:
                raise HTTPException(status_code=422, detail="会话记忆必须指定会话")
            conversation = accessible_conversation(session, user, payload.conversation_id)
            if conversation.workspace_id != workspace_id:
                raise HTTPException(status_code=404, detail="会话不存在")
        memory = create_governed_memory(
            session,
            workspace=workspace,
            user=user,
            content=payload.content,
            scope=payload.scope,
            category=payload.category,
            risk_level=payload.risk_level,
            conversation_id=payload.conversation_id,
            expires_at=payload.expires_at,
        )
        return memory_response(session, memory)

    @app.get(
        "/api/v1/workspaces/{workspace_id}/memories",
        response_model=MemoryListResponse,
    )
    def list_memories(
        workspace_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> MemoryListResponse:
        accessible_workspace(session, user, workspace_id)
        expire_due_memories(session, workspace_id)
        memories = session.scalars(
            select(Memory)
            .where(Memory.workspace_id == workspace_id, Memory.deleted_at.is_(None))
            .order_by(Memory.created_at.desc())
        ).all()
        return MemoryListResponse(items=[memory_response(session, memory) for memory in memories])

    @app.get("/api/v1/memories/{memory_id}", response_model=MemoryDetailResponse)
    def get_memory(
        memory_id: UUID, session: SessionDependency, user: CurrentUser
    ) -> MemoryDetailResponse:
        memory = accessible_memory(session, user, memory_id)
        expire_due_memories(session, memory.workspace_id)
        response = memory_response(session, memory)
        revisions = session.scalars(
            select(MemoryRevision)
            .where(MemoryRevision.memory_id == memory.id)
            .order_by(MemoryRevision.created_at)
        ).all()
        return MemoryDetailResponse(
            **response.model_dump(),
            revisions=[
                MemoryRevisionResponse(
                    content=revision.content,
                    status=revision.status,
                    change_reason=revision.change_reason,
                    created_at=revision.created_at,
                )
                for revision in revisions
            ],
        )

    def transition_memory(
        memory: Memory, session: Session, *, next_status: str, reason: str
    ) -> MemoryResponse:
        memory.status = next_status
        memory.updated_at = datetime.now(UTC)
        add_memory_revision(session, memory, status_value=next_status, reason=reason)
        return memory_response(session, memory)

    @app.post("/api/v1/memories/{memory_id}/confirm", response_model=MemoryResponse)
    def confirm_memory(
        memory_id: UUID, session: SessionDependency, user: CurrentUser
    ) -> MemoryResponse:
        memory = accessible_memory(session, user, memory_id)
        if memory.status not in {"candidate", "inactive"}:
            raise HTTPException(status_code=409, detail="当前记忆状态不能确认")
        response = transition_memory(memory, session, next_status="active", reason="confirmed")
        memory.embedding_status = "pending"
        memory.embedding_error = None
        session.commit()
        file_executor.submit(memory_indexer.process_workspace, memory.workspace_id)
        return response

    @app.post("/api/v1/memories/{memory_id}/deactivate", response_model=MemoryResponse)
    def deactivate_memory(
        memory_id: UUID, session: SessionDependency, user: CurrentUser
    ) -> MemoryResponse:
        memory = accessible_memory(session, user, memory_id)
        if memory.status != "active":
            raise HTTPException(status_code=409, detail="只有有效记忆可以停用")
        return transition_memory(memory, session, next_status="inactive", reason="deactivated")

    @app.patch("/api/v1/memories/{memory_id}", response_model=MemoryResponse)
    def update_memory(
        memory_id: UUID,
        payload: MemoryUpdateRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> MemoryResponse:
        memory = accessible_memory(session, user, memory_id)
        memory.content = payload.content.strip()
        memory.expires_at = payload.expires_at
        should_reindex = memory.status == "active"
        response = transition_memory(memory, session, next_status=memory.status, reason="edited")
        if not should_reindex:
            return response
        memory.embedding_status = "pending"
        memory.embedding_error = None
        session.commit()
        file_executor.submit(memory_indexer.process_workspace, memory.workspace_id)
        return response

    @app.post(
        "/api/v1/memories/{memory_id}/resolve-conflict",
        response_model=MemoryResponse,
    )
    def resolve_memory_conflict(
        memory_id: UUID,
        payload: MemoryConflictResolveRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> MemoryResponse:
        memory = accessible_memory(session, user, memory_id)
        conflict = session.scalar(
            select(MemoryConflict).where(
                MemoryConflict.new_memory_id == memory.id,
                MemoryConflict.status == "pending",
            )
        )
        if conflict is None or memory.status != "conflicted":
            raise HTTPException(status_code=409, detail="长期记忆没有待解决冲突")
        if payload.action not in {"retain", "replace", "coexist"}:
            raise HTTPException(status_code=422, detail="冲突解决动作无效")
        old_memory = session.get(Memory, conflict.old_memory_id)
        if old_memory is None or old_memory.deleted_at is not None:
            raise HTTPException(status_code=409, detail="冲突旧记忆已失效")
        if payload.action == "retain":
            memory.status = "inactive"
            add_memory_revision(
                session, memory, status_value="inactive", reason="conflict_retained_old"
            )
        else:
            memory.status = "active"
            add_memory_revision(
                session,
                memory,
                status_value="active",
                reason=(
                    "conflict_replaced_old" if payload.action == "replace" else "conflict_coexisted"
                ),
            )
            if payload.action == "replace":
                old_memory.status = "inactive"
                add_memory_revision(
                    session,
                    old_memory,
                    status_value="inactive",
                    reason="conflict_replaced_by_new",
                )
        conflict.status = "resolved"
        conflict.resolution = payload.action
        conflict.resolved_by_user_id = user.id
        conflict.resolved_at = datetime.now(UTC)
        should_reindex = memory.status == "active"
        if should_reindex:
            memory.embedding_status = "pending"
            memory.embedding_error = None
        session.flush()
        response = memory_response(session, memory)
        if not should_reindex:
            return response
        session.commit()
        file_executor.submit(memory_indexer.process_workspace, memory.workspace_id)
        return response

    @app.delete("/api/v1/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: UUID, session: SessionDependency, user: CurrentUser) -> Response:
        memory = accessible_memory(session, user, memory_id)
        memory.status = "deleted"
        memory.deleted_at = datetime.now(UTC)
        add_memory_revision(session, memory, status_value="deleted", reason="deleted")
        return Response(status_code=204)

    def citation_response(session: Session, citation: Citation) -> CitationResponse:
        chunk = session.get(SourceChunk, citation.source_chunk_id)
        if chunk is None:
            raise HTTPException(status_code=410, detail="引用来源已失效")
        source_captured_at: datetime | None
        if chunk.document_version_id is not None:
            version = session.get(DocumentVersion, chunk.document_version_id)
            document = session.get(Document, version.document_id) if version is not None else None
            if version is None or document is None:
                raise HTTPException(status_code=410, detail="引用来源已失效")
            source_type = "workspace_document"
            filename = document.filename
            document_version = version.version
            source_url = None
            source_captured_at = version.created_at
        elif chunk.attachment_id is not None:
            attachment = session.get(Attachment, chunk.attachment_id)
            if attachment is None:
                raise HTTPException(status_code=410, detail="引用来源已失效")
            source_type = "conversation_attachment"
            filename = attachment.filename
            document_version = None
            source_url = None
            source_captured_at = attachment.processed_at
        elif chunk.source_snapshot_id is not None:
            snapshot = session.get(SourceSnapshot, chunk.source_snapshot_id)
            if snapshot is None:
                raise HTTPException(status_code=410, detail="引用来源已失效")
            source_type = snapshot.source_type
            filename = snapshot.title
            document_version = None
            source_url = snapshot.url
            source_captured_at = snapshot.captured_at
        else:
            raise HTTPException(status_code=410, detail="引用来源已失效")
        if source_captured_at is not None and source_captured_at.tzinfo is None:
            source_captured_at = source_captured_at.replace(tzinfo=UTC)
        return CitationResponse(
            id=str(citation.id),
            label=citation.label,
            source_type=source_type,
            filename=filename,
            source_url=source_url,
            source_captured_at=source_captured_at,
            document_version=document_version,
            page_number=chunk.page_number,
            evidence_text=chunk.text[
                citation.evidence_start - chunk.start_offset :
                citation.evidence_end - chunk.start_offset
            ],
            source_hash=citation.source_hash,
        )

    @app.get(
        "/api/v1/messages/{message_id}/citations",
        response_model=CitationListResponse,
    )
    def list_message_citations(
        message_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> CitationListResponse:
        accessible_message(session, user, message_id)
        citations = session.scalars(
            select(Citation).where(Citation.message_id == message_id).order_by(Citation.label)
        ).all()
        return CitationListResponse(
            items=[citation_response(session, citation) for citation in citations]
        )

    @app.get("/api/v1/citations/{citation_id}", response_model=CitationResponse)
    def get_citation(
        citation_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> CitationResponse:
        citation = session.scalar(
            select(Citation)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Citation.workspace_id)
            .join(Workspace, Workspace.id == Citation.workspace_id)
            .where(
                Citation.id == citation_id,
                Workspace.deleted_at.is_(None),
                WorkspaceMember.user_id == user.id,
            )
        )
        if citation is None:
            raise HTTPException(status_code=404, detail="引用不存在")
        return citation_response(session, citation)

    @app.post(
        "/api/v1/messages/{message_id}/evidence-checks",
        response_model=EvidenceCheckResponse,
        status_code=201,
    )
    def create_evidence_check(
        message_id: UUID,
        payload: EvidenceCheckRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> EvidenceCheckResponse:
        message = accessible_message(session, user, message_id)
        if message.role != "assistant":
            raise HTTPException(status_code=422, detail="只能核验助手回答")
        if message.version != payload.message_version:
            raise HTTPException(status_code=409, detail="回答版本已变化，请重新选择")
        if payload.end_char <= payload.start_char or payload.end_char > len(message.content):
            raise HTTPException(status_code=422, detail="选区范围无效")
        if message.content[payload.start_char : payload.end_char] != payload.text:
            raise HTTPException(status_code=409, detail="选区文字与回答版本不一致")

        citations = session.scalars(
            select(Citation).where(Citation.message_id == message.id).order_by(Citation.label)
        ).all()
        evidence = [citation_response(session, citation) for citation in citations]
        normalized_claim = re.sub(r"\s+", "", payload.text).casefold()
        opinion = re.search(r"(?:我认为|建议|应该|值得|可能|或许)", payload.text) is not None
        matching_evidence = [
            item
            for item in evidence
            if normalized_claim in re.sub(r"\s+", "", item.evidence_text).casefold()
        ]
        if opinion:
            verdict = "not_checkable"
            reason = "该选区主要表达观点或建议，无法形成可由来源直接核验的主张。"
            used_evidence: list[CitationResponse] = []
        elif matching_evidence:
            verdict = "supported"
            reason = "原回答保存的证据片段直接包含所选主张。"
            used_evidence = matching_evidence
        elif evidence:
            claim_numbers = set(re.findall(r"\d+(?:\.\d+)?", payload.text))
            evidence_numbers = {
                value
                for item in evidence
                for value in re.findall(r"\d+(?:\.\d+)?", item.evidence_text)
            }
            if claim_numbers and evidence_numbers and claim_numbers.isdisjoint(evidence_numbers):
                verdict = "contradicted"
                reason = "原回答证据中的关键数字与所选主张不一致。"
            else:
                verdict = "insufficient"
                reason = "找到了相关来源，但现有证据不足以直接支持或反驳所选主张。"
            used_evidence = evidence
        else:
            verdict = "insufficient"
            reason = "当前回答没有可定位来源，现有证据不足。"
            used_evidence = []

        model_version = "deterministic-evidence-check-v1"
        job = VerificationJob(
            workspace_id=message.workspace_id,
            message_id=message.id,
            message_version=message.version,
            start_char=payload.start_char,
            end_char=payload.end_char,
            selected_text=payload.text,
            status="completed",
            model_version=model_version,
        )
        session.add(job)
        session.flush()
        claim = VerificationClaim(
            workspace_id=message.workspace_id,
            job_id=job.id,
            claim_text=payload.text,
            verdict=verdict,
            reason=reason,
        )
        session.add(claim)
        session.flush()
        used_ids = {item.id for item in used_evidence}
        for citation in citations:
            if str(citation.id) in used_ids:
                session.add(
                    VerificationEvidence(
                        workspace_id=message.workspace_id,
                        claim_id=claim.id,
                        citation_id=citation.id,
                    )
                )
        return EvidenceCheckResponse(
            id=str(job.id),
            claim=payload.text,
            verdict=verdict,
            reason=reason,
            evidence=used_evidence,
            model_version=model_version,
            disclaimer="结论仅表示当前可获得证据的支持度，不是绝对真伪证明。",
        )

    def accessible_run(session: Session, user: User, run_id: UUID) -> ResearchRun:
        run = session.scalar(
            select(ResearchRun)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == ResearchRun.workspace_id)
            .join(Workspace, Workspace.id == ResearchRun.workspace_id)
            .where(
                ResearchRun.id == run_id,
                WorkspaceMember.user_id == user.id,
                Workspace.deleted_at.is_(None),
            )
        )
        if run is None:
            raise HTTPException(status_code=404, detail="研究运行不存在")
        return run

    def research_source_response(
        snapshot: SourceSnapshot,
        attempts: list[WebAcquisitionAttempt],
    ) -> ResearchSourceResponse:
        """将来源快照及正文获取尝试转换为可浏览摘要"""
        return ResearchSourceResponse(
            id=str(snapshot.id),
            ordinal=snapshot.ordinal,
            title=snapshot.title,
            url=snapshot.url,
            content_kind=snapshot.content_kind,
            captured_at=snapshot.captured_at,
            content_preview=snapshot.content[:400],
            content_hash=snapshot.content_hash,
            acquisition_attempts=[
                WebAcquisitionAttemptResponse(
                    adapter_id=attempt.adapter_id,
                    adapter_version=attempt.adapter_version,
                    requested_url=attempt.requested_url,
                    final_url=attempt.final_url,
                    status=attempt.status,
                    http_status=attempt.http_status,
                    content_type=attempt.content_type,
                    warning_category=attempt.warning_category,
                    error_category=attempt.error_category,
                    retryable=attempt.retryable,
                    completeness=attempt.completeness,
                    truncated=attempt.truncated,
                    content_hash=attempt.content_hash,
                    selected_for_snapshot=attempt.source_snapshot_id == snapshot.id,
                )
                for attempt in attempts
            ],
        )

    @app.get("/api/v1/runs/{run_id}/sources", response_model=ResearchSourceListResponse)
    def list_run_sources(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ResearchSourceListResponse:
        """返回当前用户可见运行的全部网页来源"""
        run = accessible_run(session, user, run_id)
        snapshots = session.scalars(
            select(SourceSnapshot)
            .where(SourceSnapshot.run_id == run.id, SourceSnapshot.invalidated_at.is_(None))
            .order_by(SourceSnapshot.ordinal, SourceSnapshot.captured_at)
        ).all()
        attempts = session.scalars(
            select(WebAcquisitionAttempt)
            .where(WebAcquisitionAttempt.run_id == run.id)
            .order_by(
                WebAcquisitionAttempt.source_ordinal,
                WebAcquisitionAttempt.attempt_ordinal,
            )
        ).all()
        return ResearchSourceListResponse(
            items=[
                research_source_response(
                    snapshot,
                    [
                        attempt
                        for attempt in attempts
                        if attempt.source_ordinal == snapshot.ordinal
                    ],
                )
                for snapshot in snapshots
            ]
        )

    @app.get("/api/v1/sources/{source_id}", response_model=ResearchSourceDetailResponse)
    def get_research_source(
        source_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ResearchSourceDetailResponse:
        """返回当前用户有权读取的网页快照正文"""
        snapshot = session.get(SourceSnapshot, source_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="研究来源不存在")
        accessible_run(session, user, snapshot.run_id)
        attempts = session.scalars(
            select(WebAcquisitionAttempt)
            .where(
                WebAcquisitionAttempt.run_id == snapshot.run_id,
                WebAcquisitionAttempt.source_ordinal == snapshot.ordinal,
            )
            .order_by(WebAcquisitionAttempt.attempt_ordinal)
        ).all()
        summary = research_source_response(snapshot, list(attempts))
        return ResearchSourceDetailResponse(**summary.model_dump(), content=snapshot.content)

    def sandbox_output_path(storage_key: str) -> Path:
        root = resolved_settings.sandbox_output_root.resolve()
        candidate = (root / storage_key).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("非法研究产物路径")
        return candidate

    def artifact_response(artifact: Artifact) -> ArtifactResponse:
        return ArtifactResponse(
            id=str(artifact.id),
            filename=artifact.filename,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            created_at=artifact.created_at,
        )

    def sandbox_execution_response(
        session: Session, execution: SandboxExecution
    ) -> SandboxExecutionResponse:
        artifacts = session.scalars(
            select(Artifact)
            .where(
                Artifact.sandbox_execution_id == execution.id,
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at, Artifact.filename)
        ).all()
        return SandboxExecutionResponse(
            id=str(execution.id),
            run_id=str(execution.run_id),
            purpose=execution.purpose,
            code=execution.code,
            attachment_ids=execution.input_attachment_ids,
            timeout_seconds=execution.timeout_seconds,
            status=execution.status,
            stdout=execution.stdout,
            stderr=execution.stderr,
            error_message=execution.error_message,
            created_at=execution.created_at,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            artifacts=[artifact_response(artifact) for artifact in artifacts],
        )

    def tool_approval_response(
        approval: ToolApproval, tool_call: ToolCall
    ) -> ToolApprovalResponse:
        """把审批事实转换为不含原始参数的安全响应"""
        return ToolApprovalResponse(
            id=str(approval.id),
            run_id=str(approval.run_id),
            tool_call_id=str(tool_call.id),
            tool_name=tool_call.tool_name,
            risk_level=tool_call.risk_level,
            parameters_hash=approval.parameters_hash,
            safe_summary=tool_call.safe_summary,
            status=approval.status,
            expires_at=approval.expires_at,
        )

    @app.get(
        "/api/v1/runs/{run_id}/tool-approvals",
        response_model=ToolApprovalListResponse,
    )
    def list_tool_approvals(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ToolApprovalListResponse:
        """读取当前用户可见的工具审批安全摘要"""
        run = accessible_run(session, user, run_id)
        rows = session.execute(
            select(ToolApproval, ToolCall)
            .join(ToolCall, ToolCall.id == ToolApproval.tool_call_id)
            .where(ToolApproval.run_id == run.id)
            .order_by(ToolApproval.created_at)
        ).all()
        return ToolApprovalListResponse(
            items=[tool_approval_response(approval, tool_call) for approval, tool_call in rows]
        )

    def decide_tool_approval(
        approval_id: UUID,
        decision: str,
        session: Session,
        user: User,
    ) -> ToolApprovalDecisionResponse:
        """按 Workspace ACL 写入一次性审批决定并重新排队运行"""
        approval = session.scalar(
            select(ToolApproval)
            .join(ResearchRun, ResearchRun.id == ToolApproval.run_id)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == ResearchRun.workspace_id)
            .where(
                ToolApproval.id == approval_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        if approval is None:
            raise HTTPException(status_code=404, detail="工具审批不存在")
        try:
            decided = tool_execution.decide(approval_id, user.id, decision)
        except ToolApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ToolApprovalDecisionResponse(
            id=str(decided.id), run_id=str(decided.run_id), status=decided.status
        )

    @app.post(
        "/api/v1/tool-approvals/{approval_id}/approve",
        response_model=ToolApprovalDecisionResponse,
    )
    def approve_tool_approval(
        approval_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ToolApprovalDecisionResponse:
        """批准当前用户拥有的高风险工具调用"""
        return decide_tool_approval(approval_id, "approved", session, user)

    @app.post(
        "/api/v1/tool-approvals/{approval_id}/reject",
        response_model=ToolApprovalDecisionResponse,
    )
    def reject_tool_approval(
        approval_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ToolApprovalDecisionResponse:
        """拒绝当前用户拥有的高风险工具调用"""
        return decide_tool_approval(approval_id, "rejected", session, user)

    @app.get("/api/v1/runs/{run_id}/tool-runs", response_model=ToolRunListResponse)
    def list_tool_runs(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> ToolRunListResponse:
        """读取研究运行已形成的工具执行结果摘要"""
        run = accessible_run(session, user, run_id)
        rows = session.execute(
            select(ToolRun, ToolCall)
            .join(ToolCall, ToolCall.id == ToolRun.tool_call_id)
            .where(ToolRun.run_id == run.id)
            .order_by(ToolRun.created_at)
        ).all()
        return ToolRunListResponse(
            items=[
                ToolRunResponse(
                    id=str(tool_run.id),
                    tool_call_id=str(tool_call.id),
                    tool_name=tool_call.tool_name,
                    status=tool_run.status,
                    result_summary=tool_run.result_summary,
                    error_summary=tool_run.error_summary,
                )
                for tool_run, tool_call in rows
            ]
        )

    def accessible_sandbox_execution(
        session: Session, user: User, execution_id: UUID
    ) -> SandboxExecution:
        execution = session.scalar(
            select(SandboxExecution)
            .join(
                WorkspaceMember,
                WorkspaceMember.workspace_id == SandboxExecution.workspace_id,
            )
            .join(Workspace, Workspace.id == SandboxExecution.workspace_id)
            .where(
                SandboxExecution.id == execution_id,
                WorkspaceMember.user_id == user.id,
                Workspace.deleted_at.is_(None),
            )
        )
        if execution is None:
            raise HTTPException(status_code=404, detail="沙箱执行不存在")
        return execution

    def update_sandbox_todo(
        worker_session: Session,
        execution_id: UUID,
        status_value: str,
        *,
        result_summary: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """在 Sandbox 事务中更新关联 Todo，取消后清除结果摘要"""
        todo = worker_session.scalar(
            select(Todo).where(Todo.sandbox_execution_id == execution_id).with_for_update()
        )
        if todo is None or todo.status in {"completed", "skipped", "failed", "cancelled"}:
            return
        if status_value in {"timed_out", "unavailable"}:
            status_value = "failed"
        todo.status = status_value
        todo.result_summary = result_summary[:2000] if result_summary else None
        todo.failure_reason = failure_reason[:1000] if failure_reason else None
        if status_value == "running" and todo.started_at is None:
            todo.started_at = datetime.now(UTC)
        if status_value in {"completed", "skipped", "failed", "cancelled"}:
            todo.completed_at = datetime.now(UTC)

    def run_sandbox_execution(execution_id: UUID) -> None:
        try:
            with session_factory.begin() as worker_session:
                execution = worker_session.get(SandboxExecution, execution_id)
                if execution is None:
                    return
                if execution.cancel_requested_at is not None:
                    execution.status = "cancelled"
                    execution.completed_at = datetime.now(UTC)
                    update_sandbox_todo(worker_session, execution_id, "cancelled")
                    return
                execution.status = "running"
                execution.started_at = datetime.now(UTC)
                update_sandbox_todo(worker_session, execution_id, "running")
                attachment_ids = [UUID(value) for value in execution.input_attachment_ids]
                attachments = (
                    worker_session.scalars(
                        select(Attachment).where(
                            Attachment.id.in_(attachment_ids),
                            Attachment.workspace_id == execution.workspace_id,
                            Attachment.status == "ready",
                            Attachment.deleted_at.is_(None),
                        )
                    ).all()
                    if attachment_ids
                    else []
                )
                if len(attachments) != len(attachment_ids):
                    execution.status = "failed"
                    execution.error_message = "授权输入已失效"
                    execution.completed_at = datetime.now(UTC)
                    update_sandbox_todo(
                        worker_session,
                        execution_id,
                        "failed",
                        failure_reason=execution.error_message,
                    )
                    return
                input_mounts = [
                    SandboxInputMount(
                        host_path=object_store.path_for(attachment.storage_key),
                        container_name=attachment.filename,
                    )
                    for attachment in attachments
                ]
                workspace_id = execution.workspace_id
                run_id = execution.run_id
                code = execution.code
                timeout_seconds = execution.timeout_seconds

            output_key = f"{workspace_id}/{execution_id}"
            output_dir = sandbox_output_path(output_key)
            result = sandbox.execute(
                SandboxRequest(
                    code=code,
                    input_mounts=input_mounts,
                    output_dir=output_dir,
                    timeout_seconds=timeout_seconds,
                ),
                execution_key=str(execution_id),
            )

            with session_factory.begin() as worker_session:
                execution = worker_session.get(SandboxExecution, execution_id)
                if execution is None:
                    return
                execution.stdout = result.stdout
                execution.stderr = result.stderr
                execution.completed_at = datetime.now(UTC)
                if execution.cancel_requested_at is not None:
                    execution.status = "cancelled"
                    update_sandbox_todo(worker_session, execution_id, "cancelled")
                    return
                execution.status = result.status
                if result.status != "completed":
                    execution.error_message = result.stderr or "沙箱执行失败"
                    update_sandbox_todo(
                        worker_session,
                        execution_id,
                        result.status,
                        failure_reason=execution.error_message,
                    )
                    return
                update_sandbox_todo(
                    worker_session, execution_id, "completed", result_summary=result.stdout
                )
                for artifact_path in result.artifacts:
                    size_bytes = artifact_path.stat().st_size
                    if size_bytes > resolved_settings.sandbox_max_artifact_bytes:
                        execution.status = "failed"
                        execution.error_message = (
                            f"研究产物超过 {resolved_settings.sandbox_max_artifact_bytes} 字节"
                        )
                        return
                    relative_name = artifact_path.relative_to(output_dir.resolve()).as_posix()
                    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    media_type = (
                        mimetypes.guess_type(relative_name)[0] or "application/octet-stream"
                    )
                    worker_session.add(
                        Artifact(
                            workspace_id=workspace_id,
                            run_id=run_id,
                            sandbox_execution_id=execution.id,
                            filename=relative_name,
                            storage_key=f"{output_key}/{relative_name}",
                            media_type=media_type,
                            size_bytes=size_bytes,
                            sha256=digest,
                        )
                    )
        except SandboxUnavailableError as exc:
            with session_factory.begin() as worker_session:
                execution = worker_session.get(SandboxExecution, execution_id)
                if execution is not None:
                    execution.status = "unavailable"
                    execution.error_message = str(exc)
                    execution.completed_at = datetime.now(UTC)
                    update_sandbox_todo(
                        worker_session,
                        execution_id,
                        "unavailable",
                        failure_reason=execution.error_message,
                    )
        except Exception as exc:
            with session_factory.begin() as worker_session:
                execution = worker_session.get(SandboxExecution, execution_id)
                if execution is not None:
                    execution.status = "failed"
                    execution.error_message = str(exc)
                    execution.completed_at = datetime.now(UTC)
                    update_sandbox_todo(
                        worker_session,
                        execution_id,
                        "failed",
                        failure_reason=execution.error_message,
                    )

    @app.post(
        "/api/v1/runs/{run_id}/sandbox-executions",
        response_model=SandboxExecutionResponse,
        status_code=202,
    )
    def create_sandbox_execution(
        run_id: UUID,
        payload: SandboxExecutionCreateRequest,
        session: SessionDependency,
        user: CurrentUser,
    ) -> SandboxExecutionResponse:
        run = accessible_run(session, user, run_id)
        if payload.timeout_seconds > resolved_settings.sandbox_max_timeout_seconds:
            raise HTTPException(status_code=422, detail="沙箱执行时间超过部署限制")
        unique_attachment_ids = set(payload.attachment_ids)
        attachments = (
            session.scalars(
                select(Attachment).where(
                    Attachment.id.in_(unique_attachment_ids),
                    Attachment.workspace_id == run.workspace_id,
                    Attachment.conversation_id == run.conversation_id,
                    Attachment.status == "ready",
                    Attachment.deleted_at.is_(None),
                )
            ).all()
            if unique_attachment_ids
            else []
        )
        if len(attachments) != len(unique_attachment_ids):
            raise HTTPException(status_code=404, detail="沙箱输入不存在于当前会话")
        filenames = [attachment.filename for attachment in attachments]
        if len(filenames) != len(set(filenames)):
            raise HTTPException(status_code=409, detail="沙箱输入文件名不能重复")
        execution = SandboxExecution(
            workspace_id=run.workspace_id,
            run_id=run.id,
            requested_by_user_id=user.id,
            purpose=payload.purpose.strip(),
            code=payload.code,
            input_attachment_ids=[str(value) for value in payload.attachment_ids],
            timeout_seconds=payload.timeout_seconds,
            status="queued",
        )
        session.add(execution)
        session.flush()
        session.commit()
        sandbox_executor.submit(run_sandbox_execution, execution.id)
        return sandbox_execution_response(session, execution)

    @app.get(
        "/api/v1/sandbox-executions/{execution_id}",
        response_model=SandboxExecutionResponse,
    )
    def get_sandbox_execution(
        execution_id: UUID, session: SessionDependency, user: CurrentUser
    ) -> SandboxExecutionResponse:
        execution = accessible_sandbox_execution(session, user, execution_id)
        return sandbox_execution_response(session, execution)

    @app.post(
        "/api/v1/sandbox-executions/{execution_id}/cancel",
        response_model=SandboxExecutionResponse,
    )
    def cancel_sandbox_execution(
        execution_id: UUID, session: SessionDependency, user: CurrentUser
    ) -> SandboxExecutionResponse:
        execution = accessible_sandbox_execution(session, user, execution_id)
        if execution.status in {"completed", "failed", "timed_out", "cancelled", "unavailable"}:
            return sandbox_execution_response(session, execution)
        execution.cancel_requested_at = datetime.now(UTC)
        if execution.status == "queued":
            execution.status = "cancelled"
            execution.completed_at = datetime.now(UTC)
        else:
            execution.status = "cancel_requested"
        session.commit()
        if sandbox.cancel(str(execution.id)):
            execution.status = "cancelled"
            execution.completed_at = datetime.now(UTC)
            session.commit()
        return sandbox_execution_response(session, execution)

    @app.get("/api/v1/artifacts/{artifact_id}/download", response_class=FileResponse)
    def download_artifact(
        artifact_id: UUID, session: SessionDependency, user: CurrentUser
    ) -> FileResponse:
        artifact = session.scalar(
            select(Artifact)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Artifact.workspace_id)
            .join(Workspace, Workspace.id == Artifact.workspace_id)
            .where(
                Artifact.id == artifact_id,
                Artifact.deleted_at.is_(None),
                Workspace.deleted_at.is_(None),
                WorkspaceMember.user_id == user.id,
            )
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="研究产物不存在")
        path = sandbox_output_path(artifact.storage_key)
        if not path.is_file():
            raise HTTPException(status_code=410, detail="研究产物文件已失效")
        return FileResponse(path, media_type=artifact.media_type, filename=artifact.filename)

    @app.post("/api/v1/runs/{run_id}/cancel", response_model=RunStatusResponse)
    def cancel_run(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
    ) -> RunStatusResponse:
        accessible_run(session, user, run_id)
        cancelled_status = coordinator.request_cancel(run_id)
        if cancelled_status is None:
            raise HTTPException(status_code=404, detail="研究运行不存在")
        return RunStatusResponse(run_id=str(run_id), status=cancelled_status)

    @app.get("/api/v1/runs/{run_id}/events", response_class=EventSourceResponse)
    async def stream_run_events(
        run_id: UUID,
        session: SessionDependency,
        user: CurrentUser,
        last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
        after: int | None = None,
    ) -> AsyncIterator[ServerSentEvent]:
        accessible_run(session, user, run_id)
        cursor = max(last_event_id or 0, after or 0)
        while True:
            with session_factory() as event_session:
                events = event_session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                    .order_by(RunEvent.seq)
                ).all()
                run_status = event_session.scalar(
                    select(ResearchRun.status).where(ResearchRun.id == run_id)
                )
            for event in events:
                cursor = event.seq
                yield ServerSentEvent(
                    id=str(event.seq),
                    event=event.type,
                    data=encode_event_data(event.payload),
                )
            if run_status in {
                "completed",
                "partial",
                "cancelled",
                "failed",
                "waiting_approval",
            } and not events:
                break
            await anyio.sleep(0.05)

    return app


app = create_app(embedded_worker=os.getenv("DEEP_RESEARCHER_EMBEDDED_WORKER", "1") == "1")
