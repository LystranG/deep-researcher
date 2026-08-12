import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import ResearchRun, SourceChunk, SourceSnapshot
from deep_researcher.retrieval import ContextBudget, TokenEstimator


@dataclass(frozen=True)
class SourceDescriptorRequest:
    """描述一次受 Research Run 范围约束的 Chunk Descriptor 检索"""

    run_id: UUID
    snapshot_ids: tuple[UUID, ...]
    query: str
    context_budget: ContextBudget
    result_limit: int = 10
    cursor: str | None = None


@dataclass(frozen=True)
class ChunkDescriptor:
    """提供不可直接引用的稳定 Chunk 导航信息"""

    snapshot_id: UUID
    chunk_id: UUID
    ordinal: int
    heading_path: tuple[str, ...]
    token_count: int
    preview: str
    content_hash: str


@dataclass(frozen=True)
class ChunkDescriptorPage:
    """返回受 Context Budget 约束的 Descriptor 分页"""

    items: tuple[ChunkDescriptor, ...]
    consumed_tokens: int
    remaining_token_estimate: int
    cursor: str | None
    omitted_chunk_ids: tuple[UUID, ...]
    completeness: str


@dataclass(frozen=True)
class SourceWindowRequest:
    """描述按稳定 Chunk ID 读取邻近原文窗口的请求"""

    run_id: UUID
    chunk_ids: tuple[UUID, ...]
    context_budget: ContextBudget
    neighbor_window: int = 1
    cursor: str | None = None


@dataclass(frozen=True)
class SourceTextWindow:
    """返回可回到不可变 Snapshot 的完整原文窗口"""

    snapshot_id: UUID
    selected_chunk_id: UUID
    chunk_ids: tuple[UUID, ...]
    heading_path: tuple[str, ...]
    text: str
    start_offset: int
    end_offset: int
    snapshot_hash: str
    selected_chunk_hash: str


@dataclass(frozen=True)
class SourceTextPage:
    """返回按稳定窗口边界分页的原文读取结果"""

    items: tuple[SourceTextWindow, ...]
    consumed_tokens: int
    remaining_token_estimate: int
    cursor: str | None
    omitted_chunk_ids: tuple[UUID, ...]
    completeness: str


class SourceLedgerReader:
    """集中提供长来源的导航检索与原文读取 interface"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        token_estimator: TokenEstimator,
    ) -> None:
        """保存来源事实读取和 token 统计 Adapter"""
        self._session_factory = session_factory
        self._token_estimator = token_estimator

    def search_source_chunks(
        self, request: SourceDescriptorRequest
    ) -> ChunkDescriptorPage:
        """按目标相关性返回有预算边界的 Chunk Descriptor page"""
        with self._session_factory() as session:
            run = session.get(ResearchRun, request.run_id)
            if run is None:
                raise ValueError("Research Run 不存在")
            snapshots = session.scalars(
                select(SourceSnapshot).where(
                    SourceSnapshot.id.in_(request.snapshot_ids),
                    SourceSnapshot.workspace_id == run.workspace_id,
                    SourceSnapshot.run_id == run.id,
                    SourceSnapshot.invalidated_at.is_(None),
                    SourceSnapshot.content_kind == "web_page",
                )
            ).all()
            snapshot_by_id = {snapshot.id: snapshot for snapshot in snapshots}
            if set(snapshot_by_id) != set(request.snapshot_ids):
                raise ValueError("来源不属于当前 Research Run")
            chunks = session.scalars(
                select(SourceChunk)
                .where(SourceChunk.source_snapshot_id.in_(request.snapshot_ids))
                .order_by(SourceChunk.source_snapshot_id, SourceChunk.ordinal)
            ).all()
            descriptor_records = [
                (
                    self._descriptor(snapshot_by_id[chunk.source_snapshot_id], chunk),
                    chunk.text,
                )
                for chunk in chunks
                if chunk.source_snapshot_id is not None
            ]

        ranked_records = sorted(
            descriptor_records,
            key=lambda record: (
                -self._lexical_score(
                    request.query,
                    " ".join((*record[0].heading_path, record[1])),
                ),
                str(record[0].snapshot_id),
                record[0].ordinal,
            ),
        )
        ranked = [record[0] for record in ranked_records]
        capacity = request.context_budget.evidence_capacity(self._token_estimator)
        offset = int(request.cursor or "0")
        selected: list[ChunkDescriptor] = []
        permanently_omitted: list[UUID] = []
        consumed = 0
        next_offset = offset
        while next_offset < len(ranked):
            descriptor = ranked[next_offset]
            descriptor_tokens = self._descriptor_tokens(descriptor)
            if descriptor_tokens > capacity:
                permanently_omitted.append(descriptor.chunk_id)
                next_offset += 1
                continue
            if consumed + descriptor_tokens > capacity or len(selected) >= request.result_limit:
                break
            selected.append(descriptor)
            consumed += descriptor_tokens
            next_offset += 1
        deferred = tuple(descriptor.chunk_id for descriptor in ranked[next_offset:])
        omitted = (*permanently_omitted, *deferred)
        return ChunkDescriptorPage(
            items=tuple(selected),
            consumed_tokens=consumed,
            remaining_token_estimate=capacity - consumed,
            cursor=str(next_offset) if deferred else None,
            omitted_chunk_ids=omitted,
            completeness="partial" if omitted else "complete",
        )

    def read_source_chunks(self, request: SourceWindowRequest) -> SourceTextPage:
        """按稳定 Chunk ID 读取完整邻近窗口并保留精确 Snapshot locator"""
        if request.neighbor_window < 0:
            raise ValueError("邻近窗口不能小于零")
        with self._session_factory() as session:
            run = session.get(ResearchRun, request.run_id)
            if run is None:
                raise ValueError("Research Run 不存在")
            selected_chunks = session.scalars(
                select(SourceChunk).where(SourceChunk.id.in_(request.chunk_ids))
            ).all()
            selected_by_id = {chunk.id: chunk for chunk in selected_chunks}
            if set(selected_by_id) != set(request.chunk_ids):
                raise ValueError("请求包含未知 Source Chunk")
            snapshot_ids = {
                chunk.source_snapshot_id
                for chunk in selected_chunks
                if chunk.source_snapshot_id is not None
            }
            if len(snapshot_ids) != len(
                {chunk.source_snapshot_id for chunk in selected_chunks}
            ):
                raise ValueError("Source Chunk 不属于网页 Snapshot")
            snapshots = session.scalars(
                select(SourceSnapshot).where(
                    SourceSnapshot.id.in_(snapshot_ids),
                    SourceSnapshot.workspace_id == run.workspace_id,
                    SourceSnapshot.run_id == run.id,
                    SourceSnapshot.invalidated_at.is_(None),
                    SourceSnapshot.content_kind == "web_page",
                )
            ).all()
            snapshot_by_id = {snapshot.id: snapshot for snapshot in snapshots}
            if set(snapshot_by_id) != snapshot_ids:
                raise ValueError("来源不属于当前 Research Run")
            all_chunks = session.scalars(
                select(SourceChunk)
                .where(SourceChunk.source_snapshot_id.in_(snapshot_ids))
                .order_by(SourceChunk.source_snapshot_id, SourceChunk.ordinal)
            ).all()
            chunks_by_snapshot: dict[UUID, list[SourceChunk]] = {}
            for chunk in all_chunks:
                if chunk.source_snapshot_id is not None:
                    chunks_by_snapshot.setdefault(chunk.source_snapshot_id, []).append(chunk)
            windows: list[SourceTextWindow] = []
            for chunk_id in request.chunk_ids:
                selected = selected_by_id[chunk_id]
                snapshot_id = selected.source_snapshot_id
                if snapshot_id is None:
                    continue
                windows.append(
                    self._source_window(
                        snapshot_by_id[snapshot_id],
                        chunks_by_snapshot[snapshot_id],
                        selected,
                        request.neighbor_window,
                    )
                )

        capacity = request.context_budget.evidence_capacity(self._token_estimator)
        offset = int(request.cursor or "0")
        selected_windows: list[SourceTextWindow] = []
        permanently_omitted: list[UUID] = []
        consumed = 0
        next_offset = offset
        while next_offset < len(windows):
            window = windows[next_offset]
            window_tokens = self._token_estimator.count_tokens(window.text)
            if window_tokens > capacity:
                permanently_omitted.append(window.selected_chunk_id)
                next_offset += 1
                continue
            if consumed + window_tokens > capacity:
                break
            selected_windows.append(window)
            consumed += window_tokens
            next_offset += 1
        deferred = tuple(window.selected_chunk_id for window in windows[next_offset:])
        omitted = (*permanently_omitted, *deferred)
        return SourceTextPage(
            items=tuple(selected_windows),
            consumed_tokens=consumed,
            remaining_token_estimate=capacity - consumed,
            cursor=str(next_offset) if deferred else None,
            omitted_chunk_ids=omitted,
            completeness="partial" if omitted else "complete",
        )

    def _source_window(
        self,
        snapshot: SourceSnapshot,
        chunks: list[SourceChunk],
        selected: SourceChunk,
        neighbor_window: int,
    ) -> SourceTextWindow:
        """从选中 Chunk 向两侧扩展并直接切取 Snapshot 原文"""
        selected_index = next(
            index for index, chunk in enumerate(chunks) if chunk.id == selected.id
        )
        start_index = max(0, selected_index - neighbor_window)
        end_index = min(len(chunks), selected_index + neighbor_window + 1)
        covered = chunks[start_index:end_index]
        start_offset = covered[0].start_offset
        end_offset = covered[-1].end_offset
        return SourceTextWindow(
            snapshot_id=snapshot.id,
            selected_chunk_id=selected.id,
            chunk_ids=tuple(chunk.id for chunk in covered),
            heading_path=self._heading_path(snapshot.content, selected.start_offset),
            text=snapshot.content[start_offset:end_offset],
            start_offset=start_offset,
            end_offset=end_offset,
            snapshot_hash=snapshot.content_hash,
            selected_chunk_hash=selected.content_hash,
        )

    def _descriptor(
        self, snapshot: SourceSnapshot, chunk: SourceChunk
    ) -> ChunkDescriptor:
        """从不可变 Snapshot 与 Chunk 生成小型导航投影"""
        heading_path = self._heading_path(snapshot.content, chunk.start_offset)
        preview = re.sub(r"(?m)^#{1,6}[ \t]+.+?[ \t]*$", "", chunk.text).strip()
        preview = re.sub(r"\s+", " ", preview)[:240]
        return ChunkDescriptor(
            snapshot_id=snapshot.id,
            chunk_id=chunk.id,
            ordinal=chunk.ordinal,
            heading_path=heading_path,
            token_count=self._token_estimator.count_tokens(chunk.text),
            preview=preview,
            content_hash=chunk.content_hash,
        )

    def _descriptor_tokens(self, descriptor: ChunkDescriptor) -> int:
        """统计一个 Descriptor 进入模型上下文所需 token"""
        return self._token_estimator.count_tokens(
            " ".join((*descriptor.heading_path, descriptor.preview))
        )

    @staticmethod
    def _heading_path(content: str, offset: int) -> tuple[str, ...]:
        """返回指定 Snapshot offset 之前最近的 Markdown 标题路径"""
        stack: list[tuple[int, str]] = []
        for match in re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$", content):
            if match.start() > offset:
                break
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
        return tuple(title for _, title in stack)

    @staticmethod
    def _lexical_score(query: str, text: str) -> int:
        """按规范化 query 与词项命中为尾部事实排序"""
        normalized_query = query.casefold().strip()
        normalized_text = text.casefold()
        if not normalized_query:
            return 0
        score = 10 if normalized_query in normalized_text else 0
        score += sum(
            normalized_text.count(term)
            for term in re.findall(r"[\w-]+", normalized_query)
            if term
        )
        return score
