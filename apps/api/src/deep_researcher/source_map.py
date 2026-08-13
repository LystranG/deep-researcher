import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.agents.verifier import verify_evidence_candidate
from deep_researcher.models import (
    ResearchLedger,
    ResearchRun,
    SourceChunk,
    SourceMapWork,
    SourceSnapshot,
)
from deep_researcher.research_context import FrozenSource
from deep_researcher.retrieval import ContextBudget, TokenEstimator

MAP_PROMPT_VERSION = "source-map-v1"
MAP_PROMPT_POLICY = """目标：为后续整页综合分析当前 Source Chunk group

规则：
- 只输出 Chunk Digest、candidate claims、candidate span locators 和 unresolved questions
- 不能创建 Citation、Evidence Span 或已核验 Claim
- locator 必须引用输入中的 chunk_id、绝对 start/end offset 和 content_hash
- 不输出思维链"""
MAP_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "candidate_claims": {"type": "array", "items": {"type": "string"}},
        "candidate_span_locators": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "chunk_id": {"type": "string"},
                    "start_offset": {"type": "integer"},
                    "end_offset": {"type": "integer"},
                    "content_hash": {"type": "string"},
                },
                "required": ["chunk_id", "start_offset", "end_offset", "content_hash"],
            },
        },
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "candidate_claims",
        "candidate_span_locators",
        "unresolved_questions",
    ],
}


class SourceMapSpanLocator(TypedDict):
    """表示 map digest 中等待后续核验的原文定位引用"""

    chunk_id: str
    start_offset: int
    end_offset: int
    content_hash: str


class SourceMapDigest(TypedDict):
    """表示 map work 允许提交的派生结果"""

    summary: str
    candidate_claims: list[str]
    candidate_span_locators: list[SourceMapSpanLocator]
    unresolved_questions: list[str]


@dataclass(frozen=True)
class MapChunkInput:
    """表示 map 模型可见的完整 Chunk 与稳定 locator"""

    chunk_id: UUID
    ordinal: int
    text: str
    start_offset: int
    end_offset: int
    content_hash: str


@dataclass(frozen=True)
class SourceMapContext:
    """表示一次预算内 map 模型调用的完整输入"""

    run_id: UUID
    goal: str
    snapshot_id: UUID
    snapshot_hash: str
    chunks: tuple[MapChunkInput, ...]
    consumed_tokens: int
    input_capacity: int
    prompt_version: str


@dataclass(frozen=True)
class SourceMapEvidenceCandidate:
    """表示经过不可变 Chunk 回读核验的 reduce 候选"""

    claim_text: str
    source: FrozenSource


@dataclass(frozen=True)
class SourceMapReduction:
    """表示整页 reduce 的稳定覆盖与已核验候选"""

    missing_chunk_ids: tuple[str, ...]
    evidence_candidates: tuple[SourceMapEvidenceCandidate, ...]
    unsupported_claims: tuple[str, ...]


def missing_source_map_chunk_ids(works: Sequence[SourceMapWork]) -> tuple[str, ...]:
    """根据领域 work 计算稳定的缺失 Chunk 集合"""
    expected = {chunk_id for work in works for chunk_id in work.chunk_ids}
    completed = {
        chunk_id
        for work in works
        if work.status == "completed"
        for chunk_id in work.chunk_ids
    }
    return tuple(sorted(expected - completed))


def source_map_prompt(context: SourceMapContext) -> str:
    """序列化 Adapter 发送的 map 消息正文"""
    chunks = "\n\n".join(
        json.dumps(
            {
                "chunk_id": str(chunk.chunk_id),
                "ordinal": chunk.ordinal,
                "start_offset": chunk.start_offset,
                "end_offset": chunk.end_offset,
                "content_hash": chunk.content_hash,
                "text": chunk.text,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for chunk in context.chunks
    )
    return f"""{MAP_PROMPT_POLICY}

研究目标：{context.goal}
Snapshot ID：{context.snapshot_id}
Snapshot hash：{context.snapshot_hash}
Prompt version：{context.prompt_version}
Chunk group：
{chunks}
"""


def source_map_request_text(context: SourceMapContext) -> str:
    """序列化预算统计覆盖的消息与 structured schema"""
    return "\n".join(
        (
            source_map_prompt(context),
            json.dumps(MAP_RESPONSE_SCHEMA, ensure_ascii=False, sort_keys=True),
        )
    )


class SourceMapLedger:
    """规划、认领并提交可恢复 bounded map work 的深模块"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        token_estimator: TokenEstimator,
    ) -> None:
        """保存领域账本与 token estimator"""
        self._session_factory = session_factory
        self._token_estimator = token_estimator

    def plan(self, run_id: UUID, context_budget: ContextBudget) -> tuple[UUID, ...]:
        """为当前运行的长页按预算创建有限且稳定的 map work"""
        input_capacity = self._input_capacity(context_budget)
        with self._session_factory.begin() as session:
            run = session.get(ResearchRun, run_id)
            ledger = session.scalar(select(ResearchLedger).where(ResearchLedger.run_id == run_id))
            if run is None or ledger is None or run.cancel_requested_at is not None:
                return ()
            snapshots = session.scalars(
                select(SourceSnapshot)
                .where(
                    SourceSnapshot.run_id == run_id,
                    SourceSnapshot.workspace_id == run.workspace_id,
                    SourceSnapshot.content_kind == "web_page",
                    SourceSnapshot.invalidated_at.is_(None),
                )
                .order_by(SourceSnapshot.ordinal)
            ).all()
            work_ids: list[UUID] = []
            for snapshot in snapshots:
                chunks = session.scalars(
                    select(SourceChunk)
                    .where(SourceChunk.source_snapshot_id == snapshot.id)
                    .order_by(SourceChunk.ordinal)
                ).all()
                if len(chunks) <= 1:
                    continue
                for group in self._bounded_groups(
                    run.id,
                    ledger.goal,
                    snapshot,
                    chunks,
                    input_capacity,
                ):
                    input_hash = self._input_hash(
                        run_id,
                        ledger.id,
                        snapshot.content_hash,
                        group,
                    )
                    work = session.scalar(
                        select(SourceMapWork).where(
                            SourceMapWork.run_id == run_id,
                            SourceMapWork.input_hash == input_hash,
                        )
                    )
                    if work is None:
                        work = SourceMapWork(
                            workspace_id=run.workspace_id,
                            run_id=run.id,
                            ledger_id=ledger.id,
                            source_snapshot_id=snapshot.id,
                            snapshot_hash=snapshot.content_hash,
                            chunk_ids=[str(chunk.id) for chunk in group],
                            chunk_hashes=[chunk.content_hash for chunk in group],
                            input_hash=input_hash,
                            prompt_version=MAP_PROMPT_VERSION,
                            status="pending",
                        )
                        session.add(work)
                        session.flush()
                    work_ids.append(work.id)
            return tuple(work_ids)

    def pending_contexts(
        self,
        run_id: UUID,
        context_budget: ContextBudget,
    ) -> tuple[SourceMapContext, ...]:
        """回读未完成 work 的预算内模型输入"""
        input_capacity = self._input_capacity(context_budget)
        with self._session_factory() as session:
            run = session.get(ResearchRun, run_id)
            ledger = session.scalar(select(ResearchLedger).where(ResearchLedger.run_id == run_id))
            if run is None or ledger is None or run.cancel_requested_at is not None:
                return ()
            works = session.scalars(
                select(SourceMapWork)
                .where(SourceMapWork.run_id == run_id, SourceMapWork.status != "completed")
                .order_by(SourceMapWork.created_at, SourceMapWork.id)
            ).all()
            contexts: list[SourceMapContext] = []
            for work in works:
                chunk_ids = tuple(UUID(value) for value in work.chunk_ids)
                chunks = session.scalars(
                    select(SourceChunk).where(SourceChunk.id.in_(chunk_ids))
                ).all()
                chunk_by_id = {chunk.id: chunk for chunk in chunks}
                missing_chunk_ids = [
                    str(chunk_id) for chunk_id in chunk_ids if chunk_id not in chunk_by_id
                ]
                if missing_chunk_ids:
                    raise ValueError(f"map work 缺少 Source Chunk：{', '.join(missing_chunk_ids)}")
                ordered = [chunk_by_id[chunk_id] for chunk_id in chunk_ids]
                if [chunk.content_hash for chunk in ordered] != work.chunk_hashes:
                    raise ValueError("map work 的 Source Chunk hash 与账本不一致")
                context = self._context(
                    run.id,
                    ledger.goal,
                    work.source_snapshot_id,
                    work.snapshot_hash,
                    ordered,
                    input_capacity,
                    work.prompt_version,
                )
                if context.consumed_tokens > context.input_capacity:
                    raise ValueError("map work 恢复输入超过当前 Context Budget")
                contexts.append(context)
            return tuple(contexts)

    def complete(self, context: SourceMapContext, digest: SourceMapDigest) -> bool:
        """在取消边界内按稳定身份原子提交 map 派生结果"""
        input_hash = self._input_hash_from_context(context)
        normalized = self._normalize_digest(context, digest)
        with self._session_factory.begin() as session:
            run = session.get(ResearchRun, context.run_id)
            if run is None or run.cancel_requested_at is not None:
                return False
            work = session.scalar(
                select(SourceMapWork).where(
                    SourceMapWork.run_id == context.run_id,
                    SourceMapWork.input_hash == input_hash,
                )
            )
            if work is None or work.status == "completed":
                return work is not None
            work.status = "completed"
            work.digest = dict(normalized)
            work.failure_reason = None
            work.completed_at = datetime.now(UTC)
            return True

    def completed_digest(self, context: SourceMapContext) -> dict[str, object] | None:
        """在模型副作用前回读已提交结果，供 checkpoint 重放直接复用"""
        input_hash = self._input_hash_from_context(context)
        with self._session_factory() as session:
            digest = session.scalar(
                select(SourceMapWork.digest).where(
                    SourceMapWork.run_id == context.run_id,
                    SourceMapWork.input_hash == input_hash,
                    SourceMapWork.status == "completed",
                )
            )
        return dict(digest) if isinstance(digest, dict) else None

    def reduce(
        self,
        run_id: UUID,
        workspace_id: UUID,
        context_budget: ContextBudget,
    ) -> SourceMapReduction | None:
        """消费全部已提交 digest 并回读不可变 Chunk 形成 reduce 覆盖"""
        output_capacity = self._input_capacity(context_budget)
        with self._session_factory() as session:
            run = session.get(ResearchRun, run_id)
            if run is None or run.workspace_id != workspace_id:
                return None
            works = session.scalars(
                select(SourceMapWork)
                .where(
                    SourceMapWork.run_id == run_id,
                    SourceMapWork.workspace_id == workspace_id,
                )
                .order_by(SourceMapWork.created_at, SourceMapWork.id)
            ).all()
            if not works:
                return None
            missing = missing_source_map_chunk_ids(works)
            candidates: list[SourceMapEvidenceCandidate] = []
            unsupported_claims: list[str] = []
            seen: set[tuple[str, str]] = set()
            for work in works:
                if work.status != "completed" or not isinstance(work.digest, dict):
                    continue
                locators = work.digest.get("candidate_span_locators", [])
                claims = work.digest.get("candidate_claims", [])
                if not isinstance(locators, list):
                    continue
                if not isinstance(claims, list) or not all(
                    isinstance(claim, str) for claim in claims
                ):
                    continue
                supported_claims: set[str] = set()
                for locator in locators:
                    if not isinstance(locator, dict):
                        continue
                    chunk_id = locator.get("chunk_id")
                    if not isinstance(chunk_id, str):
                        continue
                    chunk = session.get(SourceChunk, UUID(chunk_id))
                    try:
                        chunk_position = work.chunk_ids.index(chunk_id)
                    except ValueError:
                        continue
                    if (
                        chunk is None
                        or chunk.workspace_id != workspace_id
                        or chunk.source_snapshot_id != work.source_snapshot_id
                        or chunk.content_hash != work.chunk_hashes[chunk_position]
                        or chunk.content_hash != locator.get("content_hash")
                    ):
                        continue
                    start = locator.get("start_offset")
                    end = locator.get("end_offset")
                    if not isinstance(start, int) or not isinstance(end, int):
                        continue
                    if start < chunk.start_offset or end > chunk.end_offset or start >= end:
                        continue
                    snapshot = (
                        session.get(SourceSnapshot, chunk.source_snapshot_id)
                        if chunk.source_snapshot_id is not None
                        else None
                    )
                    if (
                        snapshot is None
                        or snapshot.workspace_id != workspace_id
                        or snapshot.run_id != run_id
                        or snapshot.id != work.source_snapshot_id
                        or snapshot.content_hash != work.snapshot_hash
                        or snapshot.invalidated_at is not None
                    ):
                        continue
                    locator_text = snapshot.content[start:end]
                    locator_claims = [
                        claim
                        for claim in claims
                        if verify_evidence_candidate(claim, locator_text)["status"]
                        == "supported"
                    ]
                    if not locator_claims:
                        continue
                    for claim in locator_claims:
                        identity = (claim, chunk_id)
                        supported_claims.add(claim)
                        if identity in seen:
                            continue
                        seen.add(identity)
                        claim_start = start + locator_text.index(claim)
                        claim_end = claim_start + len(claim)
                        candidate = SourceMapEvidenceCandidate(
                            claim_text=claim,
                            source={
                                "source_chunk_id": chunk_id,
                                "source_snapshot_id": str(snapshot.id),
                                "text": claim,
                                "start_offset": claim_start,
                                "end_offset": claim_end,
                                "content_hash": hashlib.sha256(
                                    claim.encode()
                                ).hexdigest(),
                            },
                        )
                        proposed_answer = "\n".join(
                            [
                                *(
                                    f"{selected.claim_text} [{index}]"
                                    for index, selected in enumerate(candidates, start=1)
                                ),
                                f"{claim} [{len(candidates) + 1}]",
                            ]
                        )
                        if self._token_estimator.count_tokens(proposed_answer) <= output_capacity:
                            candidates.append(candidate)
                if not unsupported_claims and any(
                    claim for claim in claims if claim and claim not in supported_claims
                ):
                    unsupported_claims.append("unsupported_claim")
            return SourceMapReduction(
                missing,
                tuple(candidates),
                tuple(dict.fromkeys(unsupported_claims)),
            )

    def fail(self, context: SourceMapContext, reason: str) -> None:
        """记录失败并保留可恢复的未完成 work"""
        input_hash = self._input_hash_from_context(context)
        with self._session_factory.begin() as session:
            run = session.get(ResearchRun, context.run_id)
            if run is None or run.cancel_requested_at is not None:
                return
            work = session.scalar(
                select(SourceMapWork).where(
                    SourceMapWork.run_id == context.run_id,
                    SourceMapWork.input_hash == input_hash,
                )
            )
            if work is not None and work.status != "completed":
                work.status = "failed"
                work.failure_reason = reason[:1000]

    def _bounded_groups(
        self,
        run_id: UUID,
        goal: str,
        snapshot: SourceSnapshot,
        chunks: Sequence[SourceChunk],
        input_capacity: int,
    ) -> tuple[tuple[SourceChunk, ...], ...]:
        """按完整 Chunk 边界生成不超过真实模型输入容量的 group"""
        groups: list[tuple[SourceChunk, ...]] = []
        current: list[SourceChunk] = []
        for chunk in chunks:
            candidate = [*current, chunk]
            consumed = self._request_tokens(
                self._context(
                    run_id,
                    goal,
                    snapshot.id,
                    snapshot.content_hash,
                    candidate,
                    input_capacity,
                    MAP_PROMPT_VERSION,
                )
            )
            if current and consumed > input_capacity:
                groups.append(tuple(current))
                candidate = [chunk]
                consumed = self._request_tokens(
                    self._context(
                        run_id,
                        goal,
                        snapshot.id,
                        snapshot.content_hash,
                        candidate,
                        input_capacity,
                        MAP_PROMPT_VERSION,
                    )
                )
            if consumed > input_capacity:
                raise ValueError("单个 Source Chunk 超过 map Context Budget")
            current = candidate
        if current:
            groups.append(tuple(current))
        return tuple(groups)

    def _context(
        self,
        run_id: UUID,
        goal: str,
        snapshot_id: UUID,
        snapshot_hash: str,
        chunks: Sequence[SourceChunk],
        input_capacity: int,
        prompt_version: str,
    ) -> SourceMapContext:
        """根据领域事实组装 Adapter 可见的完整 map 输入"""
        context = SourceMapContext(
            run_id=run_id,
            goal=goal,
            snapshot_id=snapshot_id,
            snapshot_hash=snapshot_hash,
            chunks=tuple(
                MapChunkInput(
                    chunk_id=chunk.id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    content_hash=chunk.content_hash,
                )
                for chunk in chunks
            ),
            consumed_tokens=0,
            input_capacity=input_capacity,
            prompt_version=prompt_version,
        )
        return replace(context, consumed_tokens=self._request_tokens(context))

    def _request_tokens(self, context: SourceMapContext) -> int:
        """统计 prompt、schema、JSON 包装和 Chunk 正文的完整输入"""
        return self._token_estimator.count_tokens(source_map_request_text(context))

    @staticmethod
    def _input_capacity(context_budget: ContextBudget) -> int:
        """扣除输出预留与安全边际后返回模型输入总容量"""
        return max(
            0,
            context_budget.model_context_tokens
            - context_budget.requested_output_reserve
            - context_budget.safety_margin,
        )

    def _normalize_digest(
        self, context: SourceMapContext, digest: SourceMapDigest
    ) -> SourceMapDigest:
        """只保留允许的派生字段并校验 candidate span locator"""
        summary = digest.get("summary")
        claims = digest.get("candidate_claims")
        locators = digest.get("candidate_span_locators")
        questions = digest.get("unresolved_questions")
        if not isinstance(summary, str):
            raise ValueError("map digest summary 无效")
        if not isinstance(claims, list) or not all(isinstance(item, str) for item in claims):
            raise ValueError("map digest candidate_claims 无效")
        if not isinstance(questions, list) or not all(isinstance(item, str) for item in questions):
            raise ValueError("map digest unresolved_questions 无效")
        if not isinstance(locators, list):
            raise ValueError("map digest candidate_span_locators 无效")
        chunks = {str(chunk.chunk_id): chunk for chunk in context.chunks}
        normalized_locators: list[SourceMapSpanLocator] = []
        for value in locators:
            if not isinstance(value, dict):
                raise ValueError("map digest span locator 无效")
            chunk_id = value.get("chunk_id")
            chunk = chunks.get(chunk_id) if isinstance(chunk_id, str) else None
            if chunk is None:
                raise ValueError("map digest 引用了 group 外的 Source Chunk")
            start_offset = value.get("start_offset")
            end_offset = value.get("end_offset")
            content_hash = value.get("content_hash")
            if (
                not isinstance(start_offset, int)
                or not isinstance(end_offset, int)
                or start_offset < chunk.start_offset
                or end_offset > chunk.end_offset
                or start_offset >= end_offset
                or content_hash != chunk.content_hash
            ):
                raise ValueError("map digest span locator 与不可变 Chunk 不一致")
            normalized_locators.append(
                {
                    "chunk_id": chunk_id,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                    "content_hash": content_hash,
                }
            )
        return {
            "summary": summary,
            "candidate_claims": claims,
            "candidate_span_locators": normalized_locators,
            "unresolved_questions": questions,
        }

    def _input_hash(
        self,
        run_id: UUID,
        ledger_id: UUID,
        snapshot_hash: str,
        chunks: tuple[SourceChunk, ...],
    ) -> str:
        """根据运行、目标账本、Snapshot version 与覆盖 Chunk 生成稳定身份"""
        payload = {
            "run_id": str(run_id),
            "goal_id": str(ledger_id),
            "snapshot_hash": snapshot_hash,
            "chunks": sorted((str(chunk.id), chunk.content_hash) for chunk in chunks),
            "prompt_version": MAP_PROMPT_VERSION,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _input_hash_from_context(self, context: SourceMapContext) -> str:
        """从模型上下文重建稳定 work 身份"""
        with self._session_factory() as session:
            ledger_id = session.scalar(
                select(ResearchLedger.id).where(ResearchLedger.run_id == context.run_id)
            )
        if ledger_id is None:
            raise ValueError("Research Ledger 不存在")
        payload = {
            "run_id": str(context.run_id),
            "goal_id": str(ledger_id),
            "snapshot_hash": context.snapshot_hash,
            "chunks": sorted((str(chunk.chunk_id), chunk.content_hash) for chunk in context.chunks),
            "prompt_version": context.prompt_version,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
