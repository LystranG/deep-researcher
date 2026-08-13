import asyncio
import operator
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, NotRequired, TypedDict, cast
from uuid import UUID

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, Send, interrupt

from deep_researcher.agents.planner import PlannerOutput, ResearchBrief, TaskSpec, plan
from deep_researcher.agents.researcher import (
    DeterministicResearcherGateway,
    ResearcherGateway,
    ResearchFinding,
)
from deep_researcher.agents.verifier import VerificationResult, verify
from deep_researcher.agents.writer import WriterOutput, write
from deep_researcher.citation_validator import CitationDraft, validate_answer
from deep_researcher.model_gateway import ExtractiveModelGateway, ModelGateway
from deep_researcher.research_context import (
    FrozenResearchContext,
    FrozenSource,
    citable_sources,
    freeze_research_context,
)
from deep_researcher.run_control import CancellationToken, RunCancelledError
from deep_researcher.source_map import SourceMapContext, SourceMapLedger
from deep_researcher.tool_execution import ToolCallSnapshot, ToolExecutionService, ToolOutcome


class ResearchState(TypedDict):
    """LangGraph 内部状态，不作为用户可见事实源"""

    run_id: str
    question: str
    research_context: FrozenResearchContext
    tasks: list[TaskSpec]
    research_briefs: list[ResearchBrief]
    research_results: Annotated[list[ResearchFinding], operator.add]
    map_contexts: list[SourceMapContext]
    map_results: Annotated[list[str], operator.add]
    verification: NotRequired[VerificationResult]
    draft_answer: NotRequired[str]
    draft_deltas: NotRequired[list[str]]
    usage: NotRequired[dict[str, int | float] | None]
    answer: NotRequired[str]
    answer_deltas: NotRequired[list[str]]
    citation_drafts: NotRequired[list[CitationDraft]]
    tool_call: NotRequired[ToolCallSnapshot | None]
    tool_outcome: NotRequired[ToolOutcome]


@dataclass(frozen=True)
class GraphRuntimeContext:
    """不进入 checkpoint 的运行时 Adapter 集合"""

    model_gateway: ModelGateway
    researcher_gateway: ResearcherGateway
    tool_execution: ToolExecutionService
    source_map_ledger: SourceMapLedger | None = None
    cancellation_token: CancellationToken | None = None
    agent_role: str = "researcher"
    skill_allowed_tools: frozenset[str] | None = None


class ResearchBranchState(TypedDict):
    """单个 Send 分支携带的最小状态"""

    research_brief: ResearchBrief
    sources: list[FrozenSource]


class SourceMapBranchState(TypedDict):
    """单个 bounded map Send 分支携带的完整 Chunk group"""

    map_context: SourceMapContext


def _route_source_maps(state: ResearchState) -> str | list[Send]:
    """把有限 map work fan-out 到现有 LangGraph 执行面"""
    if not state["map_contexts"]:
        return "planner"
    return [Send("source_mapper", {"map_context": context}) for context in state["map_contexts"]]


async def _run_source_mapper(
    state: SourceMapBranchState, runtime: Runtime[GraphRuntimeContext]
) -> dict[str, list[str]]:
    """执行单个 map work 并通过领域账本幂等提交派生结果"""
    context = state["map_context"]
    if runtime.context.cancellation_token is not None:
        runtime.context.cancellation_token.raise_if_cancelled()
    ledger = runtime.context.source_map_ledger
    if ledger is None:
        return {"map_results": []}
    if ledger.completed_digest(context) is not None:
        return {"map_results": [context.snapshot_hash]}
    gateway = runtime.context.model_gateway
    try:
        digest = await gateway.acomplete_map_work(context)
        if runtime.context.cancellation_token is not None:
            runtime.context.cancellation_token.raise_if_cancelled()
        if ledger.complete(context, digest):
            return {"map_results": [context.snapshot_hash]}
    except RunCancelledError:
        raise
    except Exception as exc:
        ledger.fail(context, str(exc))
        return {"map_results": []}
    return {"map_results": []}


def _run_planner(state: ResearchState) -> PlannerOutput:
    """调用独立 Planner Module 生成有限计划"""
    return plan(state["question"])


def _route_researchers(state: ResearchState) -> list[Send]:
    """把有限任务 fan-out 到独立 Researcher 分支"""
    return [
        Send(
            "researcher",
            {
                "research_brief": brief,
                "sources": state["research_context"]["sources"],
            },
        )
        for brief in state["research_briefs"]
    ]


async def _prepare_tool_call(
    state: ResearchState, runtime: Runtime[GraphRuntimeContext]
) -> dict[str, ToolCallSnapshot | None]:
    """在副作用前创建可幂等恢复的工具审批事实"""
    return {
        "tool_call": await runtime.context.tool_execution.prepare(
            UUID(state["run_id"]),
            state["question"],
            allowed_tools=runtime.context.tool_execution.effective_allowed_tools(
                agent_role=runtime.context.agent_role,
                skill_allowed_tools=runtime.context.skill_allowed_tools,
            ),
        )
    }


def _route_after_tool_prepare(state: ResearchState) -> str | list[Send]:
    """高风险调用进入审批节点，其余研究直接 fan-out"""
    if state.get("tool_call") is not None:
        return "tool_execution"
    return _route_researchers(state)


async def _run_tool_execution(
    state: ResearchState, runtime: Runtime[GraphRuntimeContext]
) -> dict[str, ToolOutcome]:
    """通过 interrupt 等待审批，并仅在恢复后跨副作用 seam"""
    tool_call = state.get("tool_call")
    if tool_call is None:
        raise RuntimeError("工具审批节点缺少调用快照")
    resume = interrupt(
        {
            "approval_id": tool_call["approval_id"],
            "tool_call_id": tool_call["id"],
            "tool_name": tool_call["tool_name"],
            "parameters_hash": tool_call["parameters_hash"],
            "safe_summary": tool_call["safe_summary"],
            "expires_at": tool_call["expires_at"],
        }
    )
    if not isinstance(resume, dict):
        raise RuntimeError("工具审批恢复值无效")
    outcome = await runtime.context.tool_execution.execute(
        UUID(state["run_id"]),
        state["question"],
        cast(dict[str, str], resume),
        allowed_tools=runtime.context.tool_execution.effective_allowed_tools(
            agent_role=runtime.context.agent_role,
            skill_allowed_tools=runtime.context.skill_allowed_tools,
        ),
    )
    return {"tool_outcome": outcome}


async def _run_researcher(
    state: ResearchBranchState, runtime: Runtime[GraphRuntimeContext]
) -> dict[str, list[ResearchFinding]]:
    """调用独立 Researcher Module 执行一个只读分支"""
    try:
        finding = await runtime.context.researcher_gateway.research(
            state["research_brief"], state["sources"]
        )
    except Exception:
        finding = {
            "ordinal": state["research_brief"]["ordinal"],
            "status": "failed",
            "summary": "该研究分支未能取得结果",
            "source_ids": [],
            "failure_impact": "该分支证据未纳入最终结论",
        }
    return {"research_results": [finding]}


def _run_verifier(state: ResearchState) -> dict[str, VerificationResult]:
    """调用独立 Verifier Module 核对 fan-in 结果"""
    return {"verification": verify(state["research_results"])}


async def _run_writer(
    state: ResearchState, runtime: Runtime[GraphRuntimeContext]
) -> WriterOutput:
    """调用独立 Writer Module 形成内部草稿"""
    return await write(
        state["research_context"],
        state["research_results"],
        state["verification"],
        runtime.context.model_gateway,
        runtime.context.cancellation_token,
    )


def _run_citation_validator(state: ResearchState) -> dict[str, object]:
    """校验 Writer 草稿并只返回可公开的安全回答"""
    sources = citable_sources(state["research_context"])
    validation = validate_answer(
        state["draft_answer"], [source["text"] for source in sources if "text" in source]
    )
    return {
        "answer": validation.answer,
        "answer_deltas": [validation.answer],
        "citation_drafts": list(validation.citations),
    }


def build_research_graph(checkpointer: Any | None = None) -> Any:
    """构建 Flat StateGraph 及其受限 fan-out/fan-in 分支"""
    builder = StateGraph(ResearchState, context_schema=GraphRuntimeContext)
    builder.add_node("source_mapper", _run_source_mapper)
    builder.add_node("planner", _run_planner)
    builder.add_node("tool_prepare", _prepare_tool_call)
    builder.add_node("tool_execution", _run_tool_execution)
    builder.add_node("researcher", _run_researcher)
    builder.add_node("verifier", _run_verifier)
    builder.add_node("writer", _run_writer)
    builder.add_node("citation_validator", _run_citation_validator)
    builder.add_conditional_edges(START, _route_source_maps)
    builder.add_edge("source_mapper", "planner")
    builder.add_edge("planner", "tool_prepare")
    builder.add_conditional_edges("tool_prepare", _route_after_tool_prepare)
    builder.add_conditional_edges("tool_execution", _route_researchers)
    builder.add_edge("researcher", "verifier")
    builder.add_edge("verifier", "writer")
    builder.add_edge("writer", "citation_validator")
    builder.add_edge("citation_validator", END)
    return builder.compile(checkpointer=checkpointer)


class ResearchGraphRunner:
    """为领域协调器提供可恢复的 Graph 执行 seam"""

    def __init__(
        self,
        database_url: str | None = None,
        researcher_gateway: ResearcherGateway | None = None,
        *,
        max_concurrency: int = 4,
    ) -> None:
        """初始化 Graph 与单个 Research Run 的最大并发数"""
        if max_concurrency < 1:
            raise ValueError("Graph 最大并发数必须大于零")
        self._database_url = database_url
        self._researcher_gateway = researcher_gateway or DeterministicResearcherGateway()
        self._max_concurrency = max_concurrency
        self._local_checkpointer = InMemorySaver()
        self._graph = build_research_graph(self._local_checkpointer)

    async def setup_checkpointer(self) -> None:
        """在服务接收请求前初始化 PostgreSQL checkpoint schema"""
        if self._database_url and self._database_url.startswith("postgres"):
            await setup_postgres_checkpointer(self._database_url)

    async def arun(
        self,
        run_id: UUID,
        question: str,
        *,
        tool_execution: ToolExecutionService,
        map_contexts: tuple[SourceMapContext, ...] = (),
        source_map_ledger: SourceMapLedger | None = None,
        research_context: FrozenResearchContext | None = None,
        model_gateway: ModelGateway | None = None,
        resume: dict[str, str] | None = None,
        cancellation_token: CancellationToken | None = None,
        agent_role: str = "researcher",
        skill_allowed_tools: frozenset[str] | None = None,
        on_update: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> ResearchState:
        """按固定 thread_id 执行 Graph 并返回内部状态"""
        frozen_context = research_context or freeze_research_context(
            question=question,
            sources=[],
            correction=None,
            memory=None,
            conversation_leads=[],
            skills=[],
        )
        initial: ResearchState = {
            "run_id": str(run_id),
            "question": question,
            "research_context": frozen_context,
            "tasks": [],
            "research_briefs": [],
            "research_results": [],
            "map_contexts": list(map_contexts),
            "map_results": [],
        }
        if self._database_url and self._database_url.startswith("postgres"):
            async with postgres_checkpointer(self._database_url) as checkpointer:
                return await self._stream_run(
                    initial,
                    checkpointer,
                    model_gateway or ExtractiveModelGateway(),
                    tool_execution,
                    source_map_ledger,
                    resume,
                    cancellation_token,
                    agent_role,
                    skill_allowed_tools,
                    on_update,
                )
        return await self._stream_run(
            initial,
            self._local_checkpointer,
            model_gateway or ExtractiveModelGateway(),
            tool_execution,
            source_map_ledger,
            resume,
            cancellation_token,
            agent_role,
            skill_allowed_tools,
            on_update,
        )

    async def _stream_run(
        self,
        initial: ResearchState,
        checkpointer: Any | None,
        model_gateway: ModelGateway,
        tool_execution: ToolExecutionService,
        source_map_ledger: SourceMapLedger | None,
        resume: dict[str, str] | None,
        cancellation_token: CancellationToken | None,
        agent_role: str,
        skill_allowed_tools: frozenset[str] | None,
        on_update: Callable[[str, dict[str, Any]], None] | None,
    ) -> ResearchState:
        """流式运行 Graph 并收集可投影的节点更新"""
        graph = (
            self._graph
            if checkpointer is self._local_checkpointer
            else build_research_graph(checkpointer)
        )
        state: dict[str, Any] = dict(initial)
        graph_input: ResearchState | Command[Any] = (
            Command(resume=resume) if resume is not None else initial
        )
        async for update in graph.astream(
            graph_input,
            config={
                "configurable": {"thread_id": f"run:{initial['run_id']}"},
                "max_concurrency": self._max_concurrency,
            },
            context=GraphRuntimeContext(
                model_gateway=model_gateway,
                researcher_gateway=self._researcher_gateway,
                tool_execution=tool_execution,
                source_map_ledger=source_map_ledger,
                cancellation_token=cancellation_token,
                agent_role=agent_role,
                skill_allowed_tools=skill_allowed_tools,
            ),
            stream_mode="updates",
        ):
            for node_name, node_update in update.items():
                if not isinstance(node_update, dict):
                    continue
                if on_update is not None:
                    on_update(node_name, cast(dict[str, Any], node_update))
                if "tasks" in node_update:
                    state["tasks"] = node_update["tasks"]
                if "research_briefs" in node_update:
                    state["research_briefs"] = node_update["research_briefs"]
                if "research_results" in node_update:
                    state.setdefault("research_results", []).extend(
                        node_update["research_results"]
                    )
                if "map_results" in node_update:
                    state.setdefault("map_results", []).extend(node_update["map_results"])
                if "verification" in node_update:
                    state["verification"] = node_update["verification"]
                if "draft_answer" in node_update:
                    state["draft_answer"] = node_update["draft_answer"]
                if "draft_deltas" in node_update:
                    state["draft_deltas"] = node_update["draft_deltas"]
                if "usage" in node_update:
                    state["usage"] = node_update["usage"]
                if "answer" in node_update:
                    state["answer"] = node_update["answer"]
                if "answer_deltas" in node_update:
                    state["answer_deltas"] = node_update["answer_deltas"]
                if "citation_drafts" in node_update:
                    state["citation_drafts"] = node_update["citation_drafts"]
                if "tool_call" in node_update:
                    state["tool_call"] = node_update["tool_call"]
                if "tool_outcome" in node_update:
                    state["tool_outcome"] = node_update["tool_outcome"]
        return cast(ResearchState, state)

    def run(
        self,
        run_id: UUID,
        question: str,
        *,
        tool_execution: ToolExecutionService,
        map_contexts: tuple[SourceMapContext, ...] = (),
        source_map_ledger: SourceMapLedger | None = None,
        research_context: FrozenResearchContext | None = None,
        model_gateway: ModelGateway | None = None,
        resume: dict[str, str] | None = None,
        cancellation_token: CancellationToken | None = None,
        agent_role: str = "researcher",
        skill_allowed_tools: frozenset[str] | None = None,
        on_update: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> ResearchState:
        """在线程 Worker 中同步执行一次 Graph"""
        return asyncio.run(
            self.arun(
                run_id,
                question,
                research_context=research_context,
                model_gateway=model_gateway,
                tool_execution=tool_execution,
                map_contexts=map_contexts,
                source_map_ledger=source_map_ledger,
                resume=resume,
                cancellation_token=cancellation_token,
                agent_role=agent_role,
                skill_allowed_tools=skill_allowed_tools,
                on_update=on_update,
            )
        )


@asynccontextmanager
async def postgres_checkpointer(database_url: str) -> AsyncIterator[Any]:
    """创建官方 AsyncPostgresSaver，供 PostgreSQL Worker 生命周期管理"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(_checkpoint_url(database_url)) as checkpointer:
        yield checkpointer


async def setup_postgres_checkpointer(database_url: str) -> None:
    """独占初始化 PostgreSQL checkpoint schema，避免运行期 DDL 阻塞"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(_checkpoint_url(database_url)) as checkpointer:
        await checkpointer.setup()


def _checkpoint_url(database_url: str) -> str:
    """把 SQLAlchemy PostgreSQL URL 转为 checkpoint 驱动可用的连接串"""
    return database_url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
