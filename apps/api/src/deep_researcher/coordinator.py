import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.agents.planner import TaskSpec
from deep_researcher.agents.researcher import ResearchFinding
from deep_researcher.event_log import RunEventLog, RunEventRejectedError
from deep_researcher.graph import ResearchGraphRunner
from deep_researcher.model_gateway import BudgetExceededError, ModelGateway
from deep_researcher.models import (
    Attachment,
    Citation,
    ConversationSkillOverride,
    CoverageSnapshot,
    Document,
    DocumentVersion,
    EvidenceGap,
    EvidenceSpan,
    Memory,
    Message,
    ResearchClaim,
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
    SourceSnapshot,
    StopDecision,
    Todo,
    WebAcquisitionAttempt,
    WorkspaceSkillGrant,
)
from deep_researcher.quota import QuotaService
from deep_researcher.research_context import (
    FrozenMemory,
    FrozenSource,
    citable_sources,
    freeze_research_context,
)
from deep_researcher.retrieval import (
    ContextBudget,
    EmbeddingGateway,
    HybridRetrieval,
    RetrievalPage,
    RetrievalRequest,
    TokenEstimator,
)
from deep_researcher.run_control import CancellationToken, RunCancelledError
from deep_researcher.run_event_projector import RunEventProjector
from deep_researcher.source_manifest import build_source_manifest, split_source_content
from deep_researcher.stop_policy import StopPolicyInput, decide_stop
from deep_researcher.tool_execution import ToolExecutionService
from deep_researcher.web_page import WebAcquisitionGateway, WebAcquisitionResult
from deep_researcher.web_search import SearchResult, SearchUnavailableError, WebSearchGateway


class ResearchCoordinator:
    """把完整研究运行隐藏在持久化事件 interface 后。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        model_gateway: ModelGateway,
        web_search_gateway: WebSearchGateway,
        web_page_gateway: WebAcquisitionGateway,
        quota_service: QuotaService,
        tool_execution: ToolExecutionService,
        run_token_budget: int,
        run_cost_budget_usd: float,
        model_context_tokens: int,
        model_output_token_reserve: int,
        model_context_safety_margin: int,
        token_estimator: TokenEstimator,
        require_web_search_for_external_model: bool,
        step_delay_seconds: float = 0.0,
        graph_runner: ResearchGraphRunner | None = None,
        sandbox_submitter: Callable[[UUID], object] | None = None,
        sandbox_canceller: Callable[[UUID], object] | None = None,
        embedding_gateway: EmbeddingGateway | None = None,
        retrieval: HybridRetrieval | None = None,
        conversation_segment_submitter: Callable[[UUID], object] | None = None,
        research_record_submitter: Callable[[UUID], object] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._sequence_lock = Lock()
        self._event_log = RunEventLog(session_factory, process_lock=self._sequence_lock)
        self._projector = RunEventProjector(self._event_log)
        self._model_gateway = model_gateway
        self._web_search_gateway = web_search_gateway
        self._web_acquisition = web_page_gateway
        self._quota_service = quota_service
        self._tool_execution = tool_execution
        self._run_token_budget = run_token_budget
        self._run_cost_budget_usd = run_cost_budget_usd
        self._model_context_tokens = model_context_tokens
        self._model_output_token_reserve = model_output_token_reserve
        self._model_context_safety_margin = model_context_safety_margin
        self._token_estimator = token_estimator
        self._require_web_search_for_external_model = require_web_search_for_external_model
        self._step_delay_seconds = step_delay_seconds
        self._graph_runner = graph_runner or ResearchGraphRunner()
        self._sandbox_submitter = sandbox_submitter
        self._sandbox_canceller = sandbox_canceller
        self._embedding_gateway = embedding_gateway
        self._retrieval = retrieval
        self._conversation_segment_submitter = conversation_segment_submitter
        self._research_record_submitter = research_record_submitter

    def execute_run(self, run_id: UUID, *, lease_owner: str | None = None) -> None:
        """执行持有有效租约的研究运行"""
        try:
            with self._session_factory() as session:
                run = session.get(ResearchRun, run_id)
                if (
                    run is None
                    or run.status not in {"queued", "running"}
                    or (lease_owner is not None and run.lease_owner != lease_owner)
                ):
                    return
                trigger = session.get(Message, run.trigger_message_id)
                if trigger is None:
                    raise RuntimeError("触发消息不存在")
                question = trigger.content
                resume = self._tool_execution.pending_resume(run_id)
                skill_slugs, skill_allowed_tools = self._effective_skill_capabilities(
                    session, run
                )
                correction = self._latest_relevant_correction(session, run, trigger)
                retrieval_page = self._retrieve_workspace_context(
                    run,
                    question,
                    compact_conversation=trigger.content,
                    tool_schema=json.dumps(sorted(skill_allowed_tools or ())),
                )
                evidence = self._best_evidence(session, run, question, retrieval_page)
                memory = self._best_memory(session, run, question, retrieval_page)
                conversation_lead = self._best_conversation_lead(retrieval_page)

            if resume is None:
                self._append_event(
                    run_id,
                    "run_started",
                    {"message": "研究已开始"},
                    event_key="run-started",
                    lease_owner=lease_owner,
                )
            sources: list[FrozenSource] = []
            if evidence is not None:
                sources.append(evidence)
            elif conversation_lead is None and memory is None and correction is None:
                for discovered_chunk in self._search_web(
                    run_id, question, lease_owner=lease_owner
                ):
                    sources.append(discovered_chunk)
            frozen_memory: FrozenMemory | None = None
            if memory is not None:
                frozen_memory = {
                    "memory_id": str(memory.id),
                    "scope": memory.scope,
                    "content": memory.content,
                }
            research_context = freeze_research_context(
                question=question,
                sources=sources,
                correction=correction,
                memory=frozen_memory,
                conversation_leads=(
                    [conversation_lead] if conversation_lead is not None else []
                ),
                skills=skill_slugs,
            )
            for skill_slug in research_context["skills"]:
                self._append_event(
                    run_id, "skill_applied", {"slug": skill_slug}, lease_owner=lease_owner
                )
            if research_context["memory"] is not None:
                self._append_event(
                    run_id,
                    "memory_used",
                    {
                        "memory_id": research_context["memory"]["memory_id"],
                        "scope": research_context["memory"]["scope"],
                    },
                    lease_owner=lease_owner,
                )
            graph_state = self._graph_runner.run(
                run_id,
                question,
                research_context=research_context,
                model_gateway=self._model_gateway,
                tool_execution=self._tool_execution,
                resume=resume,
                agent_role="researcher",
                skill_allowed_tools=skill_allowed_tools,
                cancellation_token=CancellationToken(
                    lambda: self._is_cancel_requested(run_id, lease_owner)
                ),
                on_update=lambda node_name, update: self._handle_graph_update(
                    run_id,
                    node_name,
                    update,
                    lease_owner=lease_owner,
                ),
            )
            with self._session_factory() as session:
                current_status = session.scalar(
                    select(ResearchRun.status).where(ResearchRun.id == run_id)
                )
            if current_status == "waiting_approval":
                return
            sandbox_summaries, sandbox_failed = self._wait_for_sandbox_todos(
                run_id, lease_owner=lease_owner
            )
            self._publish_sandbox_todo_updates(run_id, lease_owner=lease_owner)
            if self._step_delay_seconds > 0:
                time.sleep(self._step_delay_seconds)

            with self._session_factory() as session:
                run = session.get(ResearchRun, run_id)
                if run is None or run.cancel_requested_at is not None:
                    self._cancel(run_id, lease_owner=lease_owner)
                    return
            answer = graph_state["answer"]
            if sandbox_summaries:
                answer = f"{answer}\n\n受限 Sandbox 计算结果：{sandbox_summaries[0]}"
            elif sandbox_failed:
                answer = f"{answer}\n\n受限 Sandbox 执行失败，未将其作为研究结论依据。"
            for answer_delta in [answer]:
                self._append_event(
                    run_id,
                    "assistant_delta",
                    {"content": answer_delta},
                    lease_owner=lease_owner,
                )
            usage = graph_state.get("usage")
            usage_summary = dict(usage) if isinstance(usage, dict) else None
            if usage_summary:
                usage_event_payload: dict[str, object] = dict(usage_summary)
                self._append_event(
                    run_id,
                    "model_usage_recorded",
                    usage_event_payload,
                    event_key="model-usage-recorded",
                    lease_owner=lease_owner,
                )
            citation_drafts = graph_state["citation_drafts"]
            completed_conversation_id: UUID | None = None
            completed_research_record_id: UUID | None = None

            with self._sequence_lock, self._session_factory.begin() as session:
                run = session.scalar(
                    select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
                )
                if (
                    run is None
                    or run.status in {"cancelled", "completed", "partial", "failed"}
                    or (lease_owner is not None and run.lease_owner != lease_owner)
                ):
                    return
                if run.cancel_requested_at is not None:
                    self._quota_service.release(session, run)
                    pending_tasks = session.scalars(
                        select(ResearchTask).where(
                            ResearchTask.run_id == run.id,
                            ResearchTask.status.in_({"pending", "running"}),
                        )
                    ).all()
                    for task in pending_tasks:
                        task.status = "cancelled"
                    pending_todos = session.scalars(
                        select(Todo).where(
                            Todo.run_id == run.id,
                            Todo.status.in_({"pending", "running"}),
                        )
                    ).all()
                    for todo in pending_todos:
                        todo.status = "cancelled"
                        todo.completed_at = datetime.now(UTC)
                    run.status = "cancelled"
                    event_type = "run_cancelled"
                    event_payload: dict[str, object] = {"message": "研究已停止"}
                else:
                    message = session.get(Message, run.assistant_message_id)
                    completed_tasks = session.scalars(
                        select(ResearchTask).where(ResearchTask.run_id == run_id)
                    ).all()
                    if message is None or not completed_tasks:
                        raise RuntimeError("研究运行状态不完整")
                    self._quota_service.settle(session, run, usage_summary)
                    message.content = answer
                    citation_ids: list[str] = []
                    persisted_citations: list[Citation] = []
                    for citation_draft in citation_drafts:
                        label = citation_draft["label"]
                        citable = citable_sources(research_context)
                        if label < 1 or label > len(citable):
                            continue
                        source = citable[label - 1]
                        citation = Citation(
                            workspace_id=run.workspace_id,
                            message_id=message.id,
                            source_chunk_id=UUID(source["source_chunk_id"]),
                            label=label,
                            answer_start=citation_draft["answer_start"],
                            answer_end=citation_draft["answer_end"],
                            evidence_start=source["start_offset"],
                            evidence_end=source["end_offset"],
                            source_hash=source["content_hash"],
                        )
                        session.add(citation)
                        session.flush()
                        citation_ids.append(str(citation.id))
                        persisted_citations.append(citation)
                    verification_status = (
                        graph_state["verification"]["status"]
                        if isinstance(graph_state.get("verification"), dict)
                        else "supported"
                    )
                    research_record = self._promote_research_record(
                        session,
                        run,
                        answer,
                        persisted_citations,
                        record_key=hashlib.sha256(question.strip().casefold().encode()).hexdigest(),
                        verification_status=verification_status,
                    )
                    if research_record is not None:
                        completed_research_record_id = research_record.id
                    final_status = self._finalize_ledger(
                        session,
                        run,
                        status="completed",
                        citation_count=len(persisted_citations),
                        verified_claim_count=1 if research_record is not None else 0,
                        verification_status=verification_status,
                        blocking_gap_descriptions=tuple(
                            task.failure_impact
                            for task in completed_tasks
                            if task.status in {"failed", "skipped"} and task.failure_impact
                        ),
                    )
                    for completed_task in completed_tasks:
                        if completed_task.status in {"pending", "running"}:
                            completed_task.status = "completed"
                            completed_task.completed_at = datetime.now(UTC)
                    remaining_todos = session.scalars(
                        select(Todo).where(
                            Todo.run_id == run.id,
                            Todo.status.in_({"pending", "running"}),
                        )
                    ).all()
                    for todo in remaining_todos:
                        todo.status = "completed"
                        todo.completed_at = datetime.now(UTC)
                    run.status = final_status
                    run.completed_at = datetime.now(UTC)
                    completed_conversation_id = run.conversation_id
                    event_type = "run_completed"
                    event_payload = {
                        "assistant_message_id": str(message.id),
                        "content": answer,
                        "citation_ids": citation_ids,
                        "research_record_id": (
                            str(research_record.id) if research_record is not None else None
                        ),
                    }
                seq = run.next_event_seq
                run.next_event_seq += 1
                session.add(
                    RunEvent(
                        workspace_id=run.workspace_id,
                        run_id=run.id,
                        seq=seq,
                        type=event_type,
                        payload=event_payload,
                    )
                )
            if (
                completed_conversation_id is not None
                and self._conversation_segment_submitter is not None
            ):
                self._conversation_segment_submitter(completed_conversation_id)
            if (
                completed_research_record_id is not None
                and self._research_record_submitter is not None
            ):
                self._research_record_submitter(completed_research_record_id)
        except RunCancelledError:
            self._cancel(run_id, lease_owner=lease_owner)
        except Exception as exc:
            self._fail(run_id, str(exc), error=exc, lease_owner=lease_owner)

    def _handle_graph_update(
        self,
        run_id: UUID,
        node_name: str,
        update: dict[str, Any],
        *,
        lease_owner: str | None,
    ) -> None:
        """幂等投影 Graph 更新并固化 Planner 任务"""
        if node_name == "planner":
            tasks = cast(list[TaskSpec], update.get("tasks", []))
            self._materialize_plan(run_id, tasks, lease_owner=lease_owner)
            self._reserve_budget(run_id, tasks, lease_owner=lease_owner)
        elif node_name == "researcher":
            self._record_researcher_outcome(
                run_id,
                cast(list[ResearchFinding], update.get("research_results", [])),
                lease_owner=lease_owner,
            )
        elif node_name == "tool_prepare" and update.get("tool_call") is not None:
            tool_call = cast(dict[str, object], update["tool_call"])
            self._append_event(
                run_id,
                "tool_approval_requested",
                {
                    "approval_id": tool_call["approval_id"],
                    "tool_call_id": tool_call["id"],
                    "tool_name": tool_call["tool_name"],
                    "parameters_hash": tool_call["parameters_hash"],
                    "safe_summary": tool_call["safe_summary"],
                    "expires_at": tool_call["expires_at"],
                },
                event_key=f"tool-approval-requested:{tool_call['id']}",
                lease_owner=lease_owner,
            )
        elif node_name == "tool_execution" and update.get("tool_outcome") is not None:
            outcome = cast(dict[str, object], update["tool_outcome"])
            event_type = (
                "tool_call_completed"
                if outcome["status"] == "completed"
                else "tool_call_rejected"
            )
            self._append_event(
                run_id,
                event_type,
                {
                    "tool_call_id": outcome["tool_call_id"],
                    "status": outcome["status"],
                    "result_summary": outcome["result_summary"],
                },
                event_key=f"tool-outcome:{outcome['tool_call_id']}",
                lease_owner=lease_owner,
            )
        self._projector.project(
            run_id, node_name, update, lease_owner=lease_owner
        )

    def _materialize_plan(
        self,
        run_id: UUID,
        tasks: list[TaskSpec],
        *,
        lease_owner: str | None,
    ) -> None:
        """按任务 ordinal 幂等固化 Graph 计划及起始事件"""
        if not tasks:
            return
        sandbox_jobs: list[UUID] = []
        with self._session_factory.begin() as session:
            run = session.get(ResearchRun, run_id)
            if (
                run is None
                or run.status not in {"queued", "running"}
                or (lease_owner is not None and run.lease_owner != lease_owner)
            ):
                return
            existing_task = session.scalar(
                select(ResearchTask.id).where(ResearchTask.run_id == run_id).limit(1)
            )
            if existing_task is None:
                trigger = session.get(Message, run.trigger_message_id)
                question = trigger.content if trigger is not None else ""
                for task in tasks:
                    research_task = ResearchTask(
                            workspace_id=run.workspace_id,
                            run_id=run.id,
                            ordinal=task["ordinal"],
                            title=task["title"],
                            role=task["role"],
                            depth=task["depth"],
                            token_budget=task["token_budget"],
                            time_budget_seconds=task["time_budget_seconds"],
                            allowed_tools=task["allowed_tools"],
                            status="running" if task["ordinal"] == 1 else "pending",
                        )
                    session.add(research_task)
                    session.flush()
                    kind = "python_sandbox" if task["role"] == "python_sandbox" else "research"
                    execution = None
                    if kind == "python_sandbox":
                        execution = SandboxExecution(
                            workspace_id=run.workspace_id,
                            run_id=run.id,
                            requested_by_user_id=run.initiated_by_user_id,
                            purpose="按研究计划完成受限计算",
                            code=self._sandbox_code(question),
                            input_attachment_ids=[],
                            timeout_seconds=task["time_budget_seconds"],
                            status="queued",
                        )
                        session.add(execution)
                        session.flush()
                        if self._sandbox_submitter is None:
                            execution.status = "unavailable"
                            execution.error_message = "Sandbox Worker 未配置"
                        sandbox_jobs.append(execution.id)
                    session.add(
                        Todo(
                            workspace_id=run.workspace_id,
                            run_id=run.id,
                            research_task_id=research_task.id,
                            ordinal=task["ordinal"],
                            title=task["title"],
                            purpose=(
                                "Agent 根据研究需要创建的 Python 计算"
                                if kind == "python_sandbox"
                                else task["title"]
                            ),
                            kind=kind,
                            status=(
                                "failed"
                                if kind == "python_sandbox" and self._sandbox_submitter is None
                                else "running" if task["ordinal"] == 1 else "pending"
                            ),
                            idempotency_key=f"plan:{task['ordinal']}:{kind}",
                            sandbox_execution_id=execution.id if execution is not None else None,
                            failure_reason=(
                                "Sandbox Worker 未配置，未执行代码"
                                if kind == "python_sandbox" and self._sandbox_submitter is None
                                else None
                            ),
                        )
                    )
        if self._sandbox_submitter is not None:
            for execution_id in sandbox_jobs:
                self._sandbox_submitter(execution_id)
        self._append_event(
            run_id,
            "plan_created",
            {
                "tasks": [
                    {
                        "ordinal": task["ordinal"],
                        "role": task["role"],
                        "title": task["title"],
                        "status": "running" if task["ordinal"] == 1 else "pending",
                    }
                    for task in tasks
                ]
            },
            event_key="graph-plan-created",
            lease_owner=lease_owner,
        )
        for task in tasks:
            self._append_event(
                run_id,
                "todo_created",
                {
                    "ordinal": task["ordinal"],
                    "title": task["title"],
                    "kind": "python_sandbox" if task["role"] == "python_sandbox" else "research",
                    "status": "running" if task["ordinal"] == 1 else "pending",
                },
                event_key=f"todo-created:{task['ordinal']}",
                lease_owner=lease_owner,
            )
        self._append_event(
            run_id,
            "task_started",
            {"ordinal": 1},
            event_key="graph-task-started:1",
            lease_owner=lease_owner,
        )

    def _sandbox_code(self, question: str) -> str:
        """从问题提取白名单算式，生成受限 Sandbox 代码"""
        match = re.search(r"(\d+(?:\s*[+\-*/]\s*\d+)+)", question)
        if match is None:
            return 'print("未找到可执行的安全算式")'
        expression = match.group(1)
        return f"print({expression})"

    def create_todo(
        self,
        run_id: UUID,
        *,
        ordinal: int,
        title: str,
        purpose: str,
        kind: str = "research",
        idempotency_key: str,
    ) -> UUID:
        """由 Agent 幂等创建一个可恢复 Todo"""
        with self._session_factory.begin() as session:
            run = session.get(ResearchRun, run_id)
            if run is None or run.cancel_requested_at is not None:
                raise RunCancelledError("研究已停止")
            todo = session.scalar(
                select(Todo).where(
                    Todo.run_id == run_id, Todo.idempotency_key == idempotency_key
                )
            )
            if todo is None:
                todo = Todo(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    ordinal=ordinal,
                    title=title,
                    purpose=purpose,
                    kind=kind,
                    status="pending",
                    idempotency_key=idempotency_key,
                )
                session.add(todo)
                session.flush()
            todo_id = todo.id
        self._append_event(
            run_id,
            "todo_created",
            {"todo_id": str(todo_id), "ordinal": ordinal, "title": title, "kind": kind},
            event_key=f"todo-created:{todo_id}",
        )
        return todo_id

    def claim_todo(self, todo_id: UUID, *, lease_owner: str, lease_seconds: int = 60) -> bool:
        """以 CAS 领取 pending Todo，租约过期后允许接管"""
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            todo = session.scalar(select(Todo).where(Todo.id == todo_id).with_for_update())
            if todo is None or todo.status not in {"pending", "running"}:
                return False
            if todo.status == "running" and todo.lease_expires_at is not None:
                if todo.lease_expires_at > now and todo.lease_owner != lease_owner:
                    return False
            todo.status = "running"
            todo.lease_owner = lease_owner
            todo.lease_expires_at = now + timedelta(seconds=lease_seconds)
            todo.started_at = todo.started_at or now
            return True

    def transition_todo(
        self,
        todo_id: UUID,
        status: str,
        *,
        lease_owner: str,
        result_summary: str | None = None,
        failure_reason: str | None = None,
    ) -> bool:
        """在租约所有者约束下完成、跳过或失败 Todo"""
        if status not in {"completed", "skipped", "failed", "cancelled"}:
            raise ValueError("Todo 终态无效")
        with self._session_factory.begin() as session:
            todo = session.scalar(select(Todo).where(Todo.id == todo_id).with_for_update())
            if todo is None or todo.lease_owner != lease_owner:
                return False
            if todo.status in {"completed", "skipped", "failed", "cancelled"}:
                return todo.status == status
            todo.status = status
            todo.result_summary = result_summary[:2000] if result_summary else None
            todo.failure_reason = failure_reason[:1000] if failure_reason else None
            todo.completed_at = datetime.now(UTC)
            return True

    def _publish_sandbox_todo_updates(
        self, run_id: UUID, *, lease_owner: str | None
    ) -> None:
        """发布已落库 Sandbox Todo 的安全状态事件"""
        with self._session_factory() as session:
            todos = session.scalars(
                select(Todo).where(Todo.run_id == run_id, Todo.kind == "python_sandbox")
            ).all()
        for todo in todos:
            self._append_event(
                run_id,
                "todo_updated",
                {
                    "todo_id": str(todo.id),
                    "kind": todo.kind,
                    "status": todo.status,
                    "result_summary": todo.result_summary,
                    "failure_reason": todo.failure_reason,
                },
                event_key=f"todo-updated:{todo.id}:{todo.status}",
                lease_owner=lease_owner,
            )

    def _wait_for_sandbox_todos(
        self,
        run_id: UUID,
        *,
        lease_owner: str | None,
    ) -> tuple[list[str], bool]:
        """等待 Agent Sandbox Todo 终态并返回可信输出与失败标记"""
        deadline = time.monotonic() + 20
        while True:
            with self._session_factory() as session:
                run = session.get(ResearchRun, run_id)
                if (
                    run is None
                    or run.cancel_requested_at is not None
                    or (lease_owner is not None and run.lease_owner != lease_owner)
                ):
                    raise RunCancelledError("研究已停止")
                todos = session.scalars(
                    select(Todo).where(Todo.run_id == run_id, Todo.kind == "python_sandbox")
                ).all()
                if not todos:
                    return [], False
                active = [todo for todo in todos if todo.status in {"pending", "running"}]
                if not active:
                    summaries = [
                        todo.result_summary.strip()
                        for todo in todos
                        if todo.status == "completed" and todo.result_summary
                    ]
                    failed = any(todo.status in {"failed", "skipped"} for todo in todos)
                    return summaries, failed
            if time.monotonic() >= deadline:
                with self._session_factory.begin() as session:
                    active = list(session.scalars(
                        select(Todo).where(
                            Todo.run_id == run_id,
                            Todo.kind == "python_sandbox",
                            Todo.status.in_({"pending", "running"}),
                        )
                    ).all())
                    for todo in active:
                        todo.status = "failed"
                        todo.failure_reason = "Sandbox 等待超时，结果未纳入结论"
                        todo.completed_at = datetime.now(UTC)
                return [], True
            time.sleep(0.05)

    def _reserve_budget(
        self,
        run_id: UUID,
        tasks: list[TaskSpec],
        *,
        lease_owner: str | None,
    ) -> None:
        """按已固化计划在模型调用前预留运行预算"""
        token_budget = sum(task["token_budget"] for task in tasks)
        if token_budget > self._run_token_budget:
            raise BudgetExceededError("研究计划超过单次运行预算")
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            if (
                run is None
                or run.cancel_requested_at is not None
                or (lease_owner is not None and run.lease_owner != lease_owner)
            ):
                raise RunCancelledError("研究已停止")
            self._quota_service.reserve(
                session,
                run,
                token_budget=token_budget,
                cost_budget_usd=self._run_cost_budget_usd,
            )

    def _record_researcher_outcome(
        self,
        run_id: UUID,
        findings: list[ResearchFinding],
        *,
        lease_owner: str | None,
    ) -> None:
        """把 Researcher 分支失败映射到用户可见任务影响"""
        failed = [finding for finding in findings if finding["status"] == "failed"]
        if not failed:
            return
        with self._session_factory.begin() as session:
            run = session.get(ResearchRun, run_id)
            if (
                run is None
                or run.cancel_requested_at is not None
                or (lease_owner is not None and run.lease_owner != lease_owner)
            ):
                return
            task = session.scalar(
                select(ResearchTask).where(
                    ResearchTask.run_id == run_id,
                    ResearchTask.role == "researcher",
                )
            )
            if task is not None:
                task.status = "failed"
                task.failure_impact = "部分研究分支失败，最终回答的证据可能不完整"

    def _is_cancel_requested(self, run_id: UUID, lease_owner: str | None) -> bool:
        """读取运行取消或租约失效状态"""
        with self._session_factory() as session:
            run = session.get(ResearchRun, run_id)
            return (
                run is None
                or run.cancel_requested_at is not None
                or run.status == "cancelled"
                or (lease_owner is not None and run.lease_owner != lease_owner)
            )

    def _retrieve_workspace_context(
        self,
        run: ResearchRun,
        query: str,
        *,
        compact_conversation: str,
        tool_schema: str,
    ) -> RetrievalPage | None:
        """按单次模型 Context Budget 统一读取当前运行可见上下文"""
        if self._retrieval is None:
            return None
        if run.initiated_by_user_id is None:
            raise RuntimeError("研究运行缺少发起用户")
        return self._retrieval.retrieve(
            RetrievalRequest(
                workspace_id=run.workspace_id,
                user_id=run.initiated_by_user_id,
                conversation_id=run.conversation_id,
                query=query,
                context_budget=ContextBudget(
                    model_context_tokens=self._model_context_tokens,
                    policy_and_prompt="仅使用可访问证据回答并保持 Citation 可核验",
                    compact_conversation=compact_conversation,
                    tool_schema=tool_schema,
                    requested_output_reserve=self._model_output_token_reserve,
                    safety_margin=self._model_context_safety_margin,
                ),
                candidate_limit=50,
                result_limit=10,
            )
        )

    @staticmethod
    def _best_conversation_lead(page: RetrievalPage | None) -> str | None:
        """返回统一检索页中排名最高的低信任会话线索"""
        if page is None:
            return None
        return next(
            (
                item.candidate.text
                for item in page.items
                if item.candidate.source_kind == "conversation_lead"
            ),
            None,
        )

    def _best_evidence(
        self,
        session: Session,
        run: ResearchRun,
        query: str,
        page: RetrievalPage | None,
    ) -> FrozenSource | None:
        """从统一检索页选择排名最高的来源并冻结可引用原始证据"""
        if page is not None:
            selected = next(
                (
                    item.candidate
                    for item in page.items
                    if item.candidate.source_kind in {"source_chunk", "research_record"}
                ),
                None,
            )
            if selected is None:
                return None
            if selected.source_kind == "research_record":
                return self._frozen_research_record_source(
                    session, run, UUID(selected.candidate_id)
                )
            selected_chunk = session.get(SourceChunk, UUID(selected.candidate_id))
            return (
                self._freeze_source_chunk(selected_chunk)
                if selected_chunk is not None
                else None
            )
        private_chunks = session.scalars(
            select(SourceChunk)
            .join(Attachment, Attachment.id == SourceChunk.attachment_id)
            .where(
                SourceChunk.workspace_id == run.workspace_id,
                SourceChunk.conversation_id == run.conversation_id,
                Attachment.status == "ready",
                Attachment.deleted_at.is_(None),
            )
        ).all()
        workspace_chunks = session.scalars(
            select(SourceChunk)
            .join(DocumentVersion, DocumentVersion.id == SourceChunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                SourceChunk.workspace_id == run.workspace_id,
                Document.workspace_id == run.workspace_id,
                Document.deleted_at.is_(None),
                DocumentVersion.version == Document.current_version,
            )
        ).all()
        candidates: dict[str, SourceChunk] = {}
        for chunk in [*workspace_chunks, *private_chunks]:
            candidates.setdefault(chunk.content_hash, chunk)
        lexical_ranked = sorted(
            ((self._lexical_score(query, chunk.text), chunk) for chunk in candidates.values()),
            key=lambda item: item[0],
            reverse=True,
        )
        if not lexical_ranked or lexical_ranked[0][0] <= 0:
            return None
        return self._freeze_source_chunk(lexical_ranked[0][1])

    @staticmethod
    def _freeze_source_chunk(chunk: SourceChunk) -> FrozenSource:
        """将可访问 SourceChunk 冻结成当前运行的引用输入"""
        return {
            "source_chunk_id": str(chunk.id),
            "text": chunk.text,
            "start_offset": chunk.start_offset,
            "end_offset": chunk.end_offset,
            "content_hash": chunk.content_hash,
        }

    def _frozen_research_record_source(
        self,
        session: Session,
        run: ResearchRun,
        record_id: UUID,
    ) -> FrozenSource | None:
        """把 ResearchRecord 命中回溯为仍可访问的不可变 EvidenceSpan"""
        record = session.get(ResearchRecord, record_id)
        if (
            record is None
            or record.workspace_id != run.workspace_id
            or record.status not in {"verified", "disputed"}
            or record.embedding_status != "ready"
            or record.deleted_at is not None
        ):
            return None
        for evidence_ref in record.evidence_refs:
            span_id = evidence_ref.get("evidence_span_id")
            if span_id is None:
                continue
            try:
                span = session.get(EvidenceSpan, UUID(str(span_id)))
            except ValueError:
                continue
            if span is None or span.workspace_id != run.workspace_id:
                continue
            chunk = session.get(SourceChunk, span.source_chunk_id)
            if (
                chunk is None
                or chunk.workspace_id != run.workspace_id
                or chunk.content_hash != span.content_hash
                or evidence_ref.get("source_hash") != span.content_hash
            ):
                continue
            if chunk.attachment_id is not None:
                attachment = session.get(Attachment, chunk.attachment_id)
                if (
                    attachment is None
                    or attachment.status != "ready"
                    or attachment.deleted_at is not None
                    or chunk.conversation_id != run.conversation_id
                ):
                    continue
            if chunk.document_version_id is not None:
                version = session.get(DocumentVersion, chunk.document_version_id)
                document = (
                    session.get(Document, version.document_id) if version is not None else None
                )
                if (
                    version is None
                    or document is None
                    or document.workspace_id != run.workspace_id
                    or document.deleted_at is not None
                    or version.version != document.current_version
                ):
                    continue
            local_start = span.start_offset - chunk.start_offset
            local_end = span.end_offset - chunk.start_offset
            if local_start < 0 or local_end > len(chunk.text) or local_start >= local_end:
                continue
            return {
                "source_chunk_id": str(chunk.id),
                "text": chunk.text[local_start:local_end],
                "start_offset": span.start_offset,
                "end_offset": span.end_offset,
                "content_hash": span.content_hash,
            }
        return None

    def _promote_research_record(
        self,
        session: Session,
        run: ResearchRun,
        claim_text: str,
        citations: list[Citation],
        *,
        record_key: str,
        verification_status: str,
    ) -> ResearchRecord | None:
        """将有精确 Citation 的已完成回答提升为不可变 Workspace Research Record"""
        if not citations or verification_status not in {"supported", "contradicted"}:
            return None
        for citation in citations:
            source_chunk = session.get(SourceChunk, citation.source_chunk_id)
            if source_chunk is None:
                return None
            if source_chunk.attachment_id is not None:
                attachment = session.get(Attachment, source_chunk.attachment_id)
                if (
                    attachment is None
                    or attachment.promoted_document_id is None
                    or attachment.deleted_at is not None
                ):
                    return None
        content_hash = hashlib.sha256(claim_text.encode()).hexdigest()
        disputed = verification_status == "contradicted"
        claim = session.scalar(
            select(ResearchClaim).where(
                ResearchClaim.run_id == run.id,
                ResearchClaim.content_hash == content_hash,
            )
        )
        if claim is None:
            claim = ResearchClaim(
                workspace_id=run.workspace_id,
                run_id=run.id,
                claim_text=claim_text,
                verdict="contradicted" if disputed else "verified",
                status="verified",
                content_hash=content_hash,
            )
            session.add(claim)
            session.flush()
        evidence_refs: list[dict[str, object]] = []
        for citation in citations:
            span = session.scalar(
                select(EvidenceSpan).where(
                    EvidenceSpan.run_id == run.id,
                    EvidenceSpan.source_chunk_id == citation.source_chunk_id,
                    EvidenceSpan.start_offset == citation.evidence_start,
                    EvidenceSpan.end_offset == citation.evidence_end,
                )
            )
            if span is None:
                span = EvidenceSpan(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    source_chunk_id=citation.source_chunk_id,
                    start_offset=citation.evidence_start,
                    end_offset=citation.evidence_end,
                    content_hash=citation.source_hash,
                )
                session.add(span)
                session.flush()
            relation = session.scalar(
                select(ResearchClaimEvidence).where(
                    ResearchClaimEvidence.claim_id == claim.id,
                    ResearchClaimEvidence.evidence_span_id == span.id,
                    ResearchClaimEvidence.relation == (
                        "contradicts" if disputed else "supports"
                    ),
                )
            )
            if relation is None:
                session.add(
                    ResearchClaimEvidence(
                        workspace_id=run.workspace_id,
                        claim_id=claim.id,
                        evidence_span_id=span.id,
                        relation="contradicts" if disputed else "supports",
                    )
                )
            evidence_refs.append(
                {
                    "evidence_span_id": str(span.id),
                    "source_chunk_id": str(span.source_chunk_id),
                    "start_offset": span.start_offset,
                    "end_offset": span.end_offset,
                    "source_hash": span.content_hash,
                }
            )
        latest_version = session.scalar(
            select(func.max(ResearchRecord.version)).where(
                ResearchRecord.workspace_id == run.workspace_id,
                ResearchRecord.record_key == record_key,
            )
        )
        latest_record = session.scalar(
            select(ResearchRecord)
            .where(
                ResearchRecord.workspace_id == run.workspace_id,
                ResearchRecord.record_key == record_key,
            )
            .order_by(ResearchRecord.version.desc())
        )
        if latest_record is not None:
            latest_record.status = "disputed" if disputed else "superseded"
        record = ResearchRecord(
            workspace_id=run.workspace_id,
            run_id=run.id,
            claim_id=claim.id,
            record_key=record_key,
            version=(latest_version or 0) + 1,
            claim_text=claim_text,
            claim_kind="fact",
            status="disputed" if disputed else "verified",
            supersedes_id=(
                latest_record.id if latest_record is not None and not disputed else None
            ),
            content_hash=content_hash,
            evidence_refs=evidence_refs,
        )
        session.add(record)
        session.flush()
        return record

    def _effective_skill_capabilities(
        self, session: Session, run: ResearchRun
    ) -> tuple[list[str], frozenset[str] | None]:
        """读取会话生效 Skill 及其允许工具集合"""
        if run.initiated_by_user_id is None:
            return [], None
        rows = session.execute(
            select(
                SkillPackage.slug,
                SkillVersion.manifest,
                WorkspaceSkillGrant.enabled,
                ConversationSkillOverride.enabled,
            )
            .join(SkillInstallation, SkillInstallation.skill_package_id == SkillPackage.id)
            .join(SkillVersion, SkillVersion.id == SkillInstallation.skill_version_id)
            .join(
                WorkspaceSkillGrant,
                WorkspaceSkillGrant.skill_installation_id == SkillInstallation.id,
            )
            .outerjoin(
                ConversationSkillOverride,
                and_(
                    ConversationSkillOverride.conversation_id == run.conversation_id,
                    ConversationSkillOverride.skill_installation_id == SkillInstallation.id,
                ),
            )
            .where(
                SkillInstallation.user_id == run.initiated_by_user_id,
                WorkspaceSkillGrant.workspace_id == run.workspace_id,
            )
            .order_by(SkillPackage.slug)
        ).all()
        if not rows:
            return [], None
        slugs: list[str] = []
        allowed_tools: set[str] = set()
        for slug, manifest, workspace_enabled, conversation_enabled in rows:
            enabled = workspace_enabled and (
                conversation_enabled if conversation_enabled is not None else True
            )
            if not enabled:
                continue
            slugs.append(slug)
            manifest_tools = manifest.get("allowed_tools", [])
            if isinstance(manifest_tools, list):
                allowed_tools.update(tool for tool in manifest_tools if isinstance(tool, str))
        return slugs, frozenset(allowed_tools)

    def _search_web(
        self, run_id: UUID, query: str, *, lease_owner: str | None = None
    ) -> list[FrozenSource]:
        """发现多个网页候选并持久化可读取的正文快照"""
        self._append_event(
            run_id,
            "tool_started",
            {
                "tool": "brave_web_search",
                "query_hash": hashlib.sha256(query.encode()).hexdigest(),
                "safe_summary": "执行网页搜索",
            },
            lease_owner=lease_owner,
        )
        try:
            results = self._web_search_gateway.search(query, count=5)
        except SearchUnavailableError as exc:
            self._append_event(
                run_id,
                "tool_skipped",
                {"tool": "brave_web_search", "reason": str(exc)},
                lease_owner=lease_owner,
            )
            if self._require_web_search_for_external_model:
                raise
            return []
        if not results:
            self._append_event(
                run_id,
                "tool_completed",
                {"tool": "brave_web_search", "result_count": 0},
                lease_owner=lease_owner,
            )
            return []

        prepared: list[tuple[int, SearchResult, WebAcquisitionResult | None]] = []
        for ordinal, result in enumerate(results[:5], start=1):
            acquisition: WebAcquisitionResult | None = None
            if ordinal <= 3:
                acquisition = self._web_acquisition.acquire(
                    result["url"],
                    should_stop=lambda: self._is_cancel_requested(run_id, lease_owner),
                )
            prepared.append((ordinal, result, acquisition))

        sources: list[FrozenSource] = []
        discovered: list[dict[str, object]] = []
        with self._session_factory.begin() as session:
            run = session.get(ResearchRun, run_id)
            if run is None or run.cancel_requested_at is not None:
                return []
            ledger = session.scalar(
                select(ResearchLedger).where(ResearchLedger.run_id == run.id)
            )
            for ordinal, result, acquisition in prepared:
                selected = acquisition.selected if acquisition is not None else None
                page_content = selected.content if selected is not None else ""
                page_title = selected.title if selected is not None else ""
                content_kind = "web_page" if page_content else "search_snippet"
                content = page_content or result["snippet"]
                title = page_title or result["title"]
                content_hash = hashlib.sha256(content.encode()).hexdigest()
                source_url = (
                    selected.final_url
                    if selected is not None and selected.final_url is not None
                    else result["url"]
                )
                snapshot = SourceSnapshot(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    source_type="web",
                    content_kind=content_kind,
                    ordinal=ordinal,
                    title=title,
                    url=source_url,
                    content=content,
                    content_hash=content_hash,
                )
                session.add(snapshot)
                session.flush()
                if acquisition is not None:
                    for attempt_ordinal, attempt in enumerate(acquisition.attempts, start=1):
                        session.add(
                            WebAcquisitionAttempt(
                                workspace_id=run.workspace_id,
                                run_id=run.id,
                                source_snapshot_id=(
                                    snapshot.id if attempt is selected else None
                                ),
                                source_ordinal=ordinal,
                                attempt_ordinal=attempt_ordinal,
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
                            )
                        )
                    if acquisition.selected is None and acquisition.attempts and ledger is not None:
                        failure_summary = ", ".join(
                            f"{attempt.adapter_id}={attempt.error_category or attempt.status}"
                            for attempt in acquisition.attempts
                        )
                        session.add(
                            EvidenceGap(
                                workspace_id=run.workspace_id,
                                ledger_id=ledger.id,
                                description=f"网页正文获取失败：{failure_summary}",
                                status="open",
                            )
                        )
                if content_kind == "web_page":
                    persisted_chunks: list[SourceChunk] = []
                    for draft in split_source_content(content):
                        chunk = SourceChunk(
                            workspace_id=run.workspace_id,
                            conversation_id=None,
                            attachment_id=None,
                            document_version_id=None,
                            source_snapshot_id=snapshot.id,
                            ordinal=draft.ordinal,
                            text=draft.text,
                            page_number=None,
                            start_offset=draft.start_offset,
                            end_offset=draft.end_offset,
                            content_hash=draft.content_hash,
                        )
                        session.add(chunk)
                        persisted_chunks.append(chunk)
                    session.flush()
                    if len(persisted_chunks) == 1:
                        sources.append(self._freeze_source_chunk(persisted_chunks[0]))
                    else:
                        selected_attempt = selected
                        warnings = (
                            (selected_attempt.warning_category,)
                            if selected_attempt is not None
                            and selected_attempt.warning_category is not None
                            else ()
                        )
                        manifest = build_source_manifest(
                            snapshot,
                            persisted_chunks,
                            token_estimator=self._token_estimator,
                            adapter_id=(
                                selected_attempt.adapter_id
                                if selected_attempt is not None
                                else None
                            ),
                            adapter_version=(
                                selected_attempt.adapter_version
                                if selected_attempt is not None
                                else None
                            ),
                            completeness=(
                                selected_attempt.completeness
                                if selected_attempt is not None
                                else "unknown"
                            ),
                            warnings=warnings,
                        )
                        sources.append(
                            {
                                "source_chunk_id": str(persisted_chunks[0].id),
                                "source_snapshot_id": str(snapshot.id),
                                "manifest": manifest,
                            }
                        )
                discovered.append(
                    {
                        "source_id": str(snapshot.id),
                        "ordinal": ordinal,
                        "title": title,
                        "url": source_url,
                        "content_kind": content_kind,
                    }
                )

        for source in discovered:
            self._append_event(
                run_id,
                "source_discovered",
                source,
                lease_owner=lease_owner,
            )
        self._append_event(
            run_id,
            "tool_completed",
            {
                "tool": "brave_web_search",
                "result_count": len(results),
                "readable_count": len(sources),
            },
            lease_owner=lease_owner,
        )
        return sources

    def _latest_relevant_correction(
        self,
        session: Session,
        run: ResearchRun,
        trigger: Message,
    ) -> str | None:
        prior_messages = session.scalars(
            select(Message)
            .where(
                Message.conversation_id == run.conversation_id,
                Message.role == "user",
                Message.id != trigger.id,
                Message.deleted_at.is_(None),
            )
            .order_by(Message.created_at.desc())
        ).all()
        for message in prior_messages:
            if not re.match(r"^\s*(纠正|更正)\s*[:：]", message.content):
                continue
            if self._lexical_score(trigger.content, message.content) <= 0:
                continue
            return re.sub(r"^\s*(纠正|更正)\s*[:：]\s*", "", message.content)
        return None

    def _best_memory(
        self,
        session: Session,
        run: ResearchRun,
        query: str,
        page: RetrievalPage | None,
    ) -> Memory | None:
        initiated_by_user_id = run.initiated_by_user_id
        if initiated_by_user_id is None:
            raise RuntimeError("研究运行缺少发起用户")
        if page is not None:
            selected = next(
                (
                    item.candidate
                    for item in page.items
                    if item.candidate.source_kind == "memory"
                ),
                None,
            )
            return session.get(Memory, UUID(selected.candidate_id)) if selected else None
        now = datetime.now(UTC)
        memories = session.scalars(
            select(Memory).where(
                Memory.workspace_id == run.workspace_id,
                Memory.status == "active",
                Memory.deleted_at.is_(None),
                (Memory.expires_at.is_(None) | (Memory.expires_at > now)),
                (
                    (Memory.scope == "workspace")
                    | ((Memory.scope == "user") & (Memory.user_id == initiated_by_user_id))
                    | (
                        (Memory.scope == "conversation")
                        & (Memory.conversation_id == run.conversation_id)
                    )
                ),
            )
        ).all()
        lexical_ranked = sorted(
            ((self._lexical_score(query, memory.content), memory) for memory in memories),
            key=lambda item: item[0],
            reverse=True,
        )
        if not lexical_ranked or lexical_ranked[0][0] <= 0:
            return None
        return lexical_ranked[0][1]

    @staticmethod
    def _lexical_score(query: str, text: str) -> int:
        normalized_query = query.casefold()
        normalized_text = text.casefold()
        words = [word for word in re.findall(r"[a-z0-9_-]{2,}", normalized_query)]
        cjk_sequences = re.findall(r"[\u3400-\u9fff]+", normalized_query)
        cjk_bigrams = [
            sequence[index : index + 2]
            for sequence in cjk_sequences
            for index in range(max(0, len(sequence) - 1))
        ]
        terms = [*words, *cjk_bigrams]
        return sum(normalized_text.count(term) for term in terms)

    def request_cancel(self, run_id: UUID) -> str | None:
        """持久化取消，并立即终止等待审批的运行"""
        waiting_approval = False
        result_status: str | None = None
        sandbox_execution_ids: list[UUID] = []
        with self._sequence_lock, self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            if run is None:
                return None
            if run.status in {"completed", "partial", "cancelled", "failed"}:
                return run.status
            waiting_approval = run.status == "waiting_approval"
            cancelled_at = datetime.now(UTC)
            run.cancel_requested_at = cancelled_at
            self._quota_service.release(session, run)
            tasks = session.scalars(
                select(ResearchTask).where(
                    ResearchTask.run_id == run.id,
                    ResearchTask.status.in_({"pending", "running"}),
                )
            ).all()
            for task in tasks:
                task.status = "cancelled"
                task.completed_at = cancelled_at
            todos = session.scalars(
                select(Todo).where(
                    Todo.run_id == run.id,
                    Todo.status.in_({"pending", "running"}),
                )
            ).all()
            for todo in todos:
                todo.status = "cancelled"
                todo.completed_at = cancelled_at
                if todo.sandbox_execution_id is not None:
                    sandbox_execution_ids.append(todo.sandbox_execution_id)
            executions = (
                session.scalars(
                    select(SandboxExecution).where(
                        SandboxExecution.id.in_(sandbox_execution_ids)
                    )
                ).all()
                if sandbox_execution_ids
                else []
            )
            for execution in executions:
                execution.cancel_requested_at = cancelled_at
            run.status = "cancelled"
            run.completed_at = cancelled_at
            self._finalize_ledger(session, run, status="cancelled")
            seq = run.next_event_seq
            run.next_event_seq += 1
            session.add(
                RunEvent(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    seq=seq,
                    type="run_cancelled",
                    payload={"message": "研究已停止"},
                )
            )
            result_status = run.status
        if waiting_approval:
            self._tool_execution.cancel_pending(run_id)
        for execution_id in sandbox_execution_ids:
            if self._sandbox_canceller is not None:
                self._sandbox_canceller(execution_id)
        return result_status

    def record_sandbox_todo_outcome(
        self,
        execution_id: UUID,
        status: str,
        *,
        result_summary: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """按 Sandbox Execution 幂等投影关联 Todo 的状态和安全摘要"""
        mapped_status = "failed" if status in {"failed", "timed_out", "unavailable"} else status
        todo_id: UUID | None = None
        run_id: UUID | None = None
        with self._session_factory.begin() as session:
            todo = session.scalar(
                select(Todo).where(Todo.sandbox_execution_id == execution_id).with_for_update()
            )
            if todo is None or todo.status in {"completed", "skipped", "failed", "cancelled"}:
                return
            run = session.get(ResearchRun, todo.run_id)
            if run is None or run.cancel_requested_at is not None or run.status == "cancelled":
                mapped_status = "cancelled"
                result_summary = None
                failure_reason = None
            todo.status = mapped_status
            todo.result_summary = result_summary[:2000] if result_summary else None
            todo.failure_reason = failure_reason[:1000] if failure_reason else None
            now = datetime.now(UTC)
            if mapped_status == "running" and todo.started_at is None:
                todo.started_at = now
            if mapped_status in {"completed", "skipped", "failed", "cancelled"}:
                todo.completed_at = now
            todo_id = todo.id
            run_id = todo.run_id
        if todo_id is not None and run_id is not None:
            self._append_event(
                run_id,
                "todo_updated",
                {
                    "todo_id": str(todo_id),
                    "status": mapped_status,
                    "result_summary": result_summary,
                    "failure_reason": failure_reason,
                },
                event_key=f"todo-updated:{todo_id}:{mapped_status}",
            )

    def _append_event(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict[str, object],
        *,
        event_key: str | None = None,
        lease_owner: str | None = None,
    ) -> None:
        """追加可按业务键去重的 RunEvent"""
        try:
            self._event_log.append(
                run_id,
                event_type,
                payload,
                event_key=event_key,
                lease_owner=lease_owner,
            )
        except RunEventRejectedError:
            return

    def _cancel(self, run_id: UUID, *, lease_owner: str | None = None) -> None:
        with self._sequence_lock, self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            if (
                run is None
                or run.status in {"cancelled", "completed", "partial", "failed"}
                or (lease_owner is not None and run.lease_owner != lease_owner)
                ):
                return
            run.status = "cancelled"
            self._quota_service.release(session, run)
            self._finalize_ledger(session, run, status="cancelled")
            pending_tasks = session.scalars(
                select(ResearchTask).where(
                    ResearchTask.run_id == run.id,
                    ResearchTask.status.in_({"pending", "running"}),
                )
            ).all()
            for task in pending_tasks:
                task.status = "cancelled"
            seq = run.next_event_seq
            run.next_event_seq += 1
            session.add(
                RunEvent(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    seq=seq,
                    type="run_cancelled",
                    payload={"message": "研究已停止"},
                )
            )

    def _fail(
        self,
        run_id: UUID,
        detail: str,
        *,
        error: Exception | None = None,
        lease_owner: str | None = None,
    ) -> None:
        """把运行失败映射为用户可见的任务影响和终态事件"""
        safe_detail = detail[:500]
        with self._sequence_lock, self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            if (
                run is None
                or run.status in {"cancelled", "completed", "partial", "failed"}
                or (lease_owner is not None and run.lease_owner != lease_owner)
            ):
                return
            run.status = "failed"
            self._quota_service.release(session, run)
            public_message: str | None = None
            if isinstance(error, SearchUnavailableError):
                if safe_detail == "未配置 Brave Search API 凭证":
                    public_message = (
                        "网页检索不可用：未配置 Brave Search API 凭证。"
                        "请配置 DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY 后重试。"
                    )
                else:
                    public_message = f"网页检索不可用：{safe_detail}。请稍后重试。"
                assistant_message = session.get(Message, run.assistant_message_id)
                if assistant_message is not None:
                    assistant_message.content = public_message
            active_tasks = session.scalars(
                select(ResearchTask).where(
                    ResearchTask.run_id == run.id,
                    ResearchTask.status.in_({"pending", "running"}),
                )
            ).all()
            budget_exhausted = isinstance(error, BudgetExceededError)
            for task in active_tasks:
                if budget_exhausted and task.status == "pending":
                    task.status = "skipped"
                    task.failure_impact = "上游任务预算耗尽，未执行"
                else:
                    task.status = "failed"
                    task.failure_impact = (
                        "当前任务预算耗尽，研究无法继续"
                        if budget_exhausted
                        else "研究运行失败，任务未能完成"
                    )
            self._finalize_ledger(
                session,
                run,
                status="failed",
                blocking_gap_descriptions=tuple(
                    dict.fromkeys(
                        task.failure_impact
                        for task in active_tasks
                        if task.failure_impact
                    )
                ),
            )
            if public_message is not None:
                assistant_seq = run.next_event_seq
                run.next_event_seq += 1
                session.add(
                    RunEvent(
                        workspace_id=run.workspace_id,
                        run_id=run.id,
                        seq=assistant_seq,
                        type="assistant_delta",
                        payload={"content": public_message},
                    )
                )
            seq = run.next_event_seq
            run.next_event_seq += 1
            session.add(
                RunEvent(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    seq=seq,
                    type="run_failed",
                    payload={"message": "研究失败", "detail": safe_detail},
                )
            )

    def _finalize_ledger(
        self,
        session: Session,
        run: ResearchRun,
        *,
        status: str,
        citation_count: int = 0,
        verified_claim_count: int = 0,
        verification_status: str | None = None,
        blocking_gap_descriptions: tuple[str, ...] = (),
    ) -> str:
        """根据终态和已固化 Citation 写入 Coverage Snapshot 与 Stop Decision"""
        ledger = session.scalar(select(ResearchLedger).where(ResearchLedger.run_id == run.id))
        if ledger is None:
            return status
        outcome = decide_stop(
            StopPolicyInput(
                requested_status=status,
                verification_status=verification_status,
                verified_claim_count=verified_claim_count,
                valid_citation_count=citation_count,
                blocking_gap_descriptions=blocking_gap_descriptions,
            )
        )
        ledger.status = outcome.status
        session.add(
            CoverageSnapshot(
                workspace_id=run.workspace_id,
                ledger_id=ledger.id,
                citation_count=citation_count,
                verified_claim_count=verified_claim_count,
                complete=outcome.complete,
            )
        )
        if not outcome.complete:
            session.add_all(
                [
                    EvidenceGap(
                        workspace_id=run.workspace_id,
                        ledger_id=ledger.id,
                        description=description,
                        status="open",
                    )
                    for description in outcome.gap_descriptions
                ]
            )
        session.add(
            StopDecision(
                workspace_id=run.workspace_id,
                ledger_id=ledger.id,
                reason=outcome.reason,
                completeness="complete" if outcome.complete else "partial",
            )
        )
        return outcome.status


def encode_event_data(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
