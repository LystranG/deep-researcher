import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from deep_researcher.models import (
    Attachment,
    Conversation,
    ConversationSegment,
    Document,
    DocumentVersion,
    Memory,
    ResearchRecord,
    SourceChunk,
)


@dataclass(frozen=True)
class RetrievalCandidate:
    """表示已经通过访问范围过滤的可检索候选"""

    candidate_id: str
    text: str
    source_kind: str
    content_hash: str
    embedding: tuple[float, ...] | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RankedCandidate:
    """表示经过融合和重排后的候选"""

    candidate: RetrievalCandidate
    score: float
    rank: int


class EmbeddingGateway(Protocol):
    """生成文档和查询向量的 Adapter"""

    @property
    def model_name(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


class RerankGateway(Protocol):
    """根据查询对候选文本进行精排的 Adapter"""

    def rerank(
        self, query: str, documents: Sequence[str], top_n: int
    ) -> list[tuple[int, float]]: ...


class SourceChunkRetrievalAdapter(Protocol):
    """按访问范围从持久化层召回可检索 SourceChunk"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]: ...


class ConversationSegmentRetrievalAdapter(Protocol):
    """按 Workspace 范围召回其他会话的低信任历史线索"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]: ...


class MemoryRetrievalAdapter(Protocol):
    """按 Workspace、User 和 Conversation scope 召回有效长期记忆"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]: ...


class ResearchRecordRetrievalAdapter(Protocol):
    """按 Workspace 范围召回可跨会话复用的研究记录"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]: ...


class LiteLLMEmbeddingGateway:
    """通过 LiteLLM 直接调用配置的 embedding 模型"""

    def __init__(self, *, api_key: str, model: str, api_base: str | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._api_base = api_base

    @property
    def model_name(self) -> str:
        """返回持久化到索引元数据的模型标识"""
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """批量生成文档向量，Provider 失败时直接抛出异常"""
        from litellm import embedding

        response = embedding(**self._request_args(texts))
        return [tuple(float(value) for value in item["embedding"]) for item in response.data]

    def embed_query(self, text: str) -> tuple[float, ...]:
        """生成查询向量，Provider 失败时直接抛出异常"""
        return self.embed_documents([text])[0]

    def _request_args(self, texts: Sequence[str]) -> dict[str, Any]:
        """组装 LiteLLM embedding 请求参数"""
        request_args: dict[str, Any] = {
            "model": self._model,
            "api_key": self._api_key,
            "input": list(texts),
        }
        if self._api_base is not None:
            request_args["api_base"] = self._api_base
        return request_args


class PostgresSourceChunkRetrievalAdapter:
    """执行 Workspace ACL 过滤、PostgreSQL FTS 与 exact pgvector 召回"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """召回当前会话私有附件和当前 Workspace 文档的 ready Chunk"""
        if limit <= 0:
            return []
        eligible = self._eligible_query(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
        )
        if session.bind is None or session.bind.dialect.name != "postgresql":
            chunks = session.scalars(
                eligible.order_by(SourceChunk.ordinal).limit(limit)
            ).all()
            return [self._candidate(chunk) for chunk in chunks]

        lexical_query = func.websearch_to_tsquery("simple", query)
        lexical_rank = func.ts_rank_cd(
            func.to_tsvector("simple", SourceChunk.text), lexical_query
        )
        lexical_rows = session.execute(
            eligible.where(
                func.to_tsvector("simple", SourceChunk.text).op("@@")(lexical_query)
            )
            .add_columns(lexical_rank.label("lexical_rank"))
            .order_by(desc(lexical_rank), SourceChunk.ordinal)
            .limit(limit)
        ).all()
        vector_distance = SourceChunk.embedding.cosine_distance(list(query_embedding))
        vector_rows = session.execute(
            eligible.where(SourceChunk.embedding.is_not(None))
            .add_columns(vector_distance.label("vector_distance"))
            .order_by(vector_distance, SourceChunk.ordinal)
            .limit(limit)
        ).all()
        candidates: dict[str, RetrievalCandidate] = {}
        for rank, row in enumerate(lexical_rows, start=1):
            chunk = row[0]
            candidates.setdefault(chunk.content_hash, self._candidate(chunk))
            candidates[chunk.content_hash].metadata["lexical_rank"] = str(rank)
        for rank, row in enumerate(vector_rows, start=1):
            chunk = row[0]
            candidates.setdefault(chunk.content_hash, self._candidate(chunk))
            candidates[chunk.content_hash].metadata["vector_rank"] = str(rank)
        return list(candidates.values())

    @staticmethod
    def _eligible_query(*, workspace_id: UUID, conversation_id: UUID) -> Any:
        """构建先于召回执行 Workspace 和资源作用域过滤的查询"""
        private_scope = and_(
            SourceChunk.attachment_id.is_not(None),
            SourceChunk.conversation_id == conversation_id,
            Attachment.id == SourceChunk.attachment_id,
            Attachment.workspace_id == workspace_id,
            Attachment.status == "ready",
            Attachment.deleted_at.is_(None),
        )
        workspace_scope = and_(
            SourceChunk.document_version_id.is_not(None),
            DocumentVersion.id == SourceChunk.document_version_id,
            Document.id == DocumentVersion.document_id,
            Document.workspace_id == workspace_id,
            Document.deleted_at.is_(None),
            DocumentVersion.version == Document.current_version,
        )
        return (
            select(SourceChunk)
            .select_from(SourceChunk)
            .outerjoin(Attachment, Attachment.id == SourceChunk.attachment_id)
            .outerjoin(DocumentVersion, DocumentVersion.id == SourceChunk.document_version_id)
            .outerjoin(Document, Document.id == DocumentVersion.document_id)
            .where(
                SourceChunk.workspace_id == workspace_id,
                SourceChunk.embedding_status == "ready",
                or_(private_scope, workspace_scope),
            )
        )

    @staticmethod
    def _candidate(chunk: SourceChunk) -> RetrievalCandidate:
        """将 ORM Chunk 转成不会携带数据库状态的检索候选"""
        return RetrievalCandidate(
            candidate_id=str(chunk.id),
            text=chunk.text,
            source_kind="source_chunk",
            content_hash=chunk.content_hash,
            embedding=tuple(float(value) for value in (chunk.embedding or [])),
        )


class PostgresConversationSegmentRetrievalAdapter:
    """执行 Workspace 过滤、全文与 exact pgvector 历史会话召回"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """召回同一 Workspace 其他有效会话的 ready 分段"""
        if limit <= 0:
            return []
        eligible = self._eligible_query(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
        )
        if session.bind is None or session.bind.dialect.name != "postgresql":
            segments = session.scalars(
                eligible.order_by(ConversationSegment.ordinal).limit(limit)
            ).all()
            return [self._candidate(segment) for segment in segments]

        lexical_query = func.websearch_to_tsquery("simple", query)
        lexical_rank = func.ts_rank_cd(
            func.to_tsvector("simple", ConversationSegment.text), lexical_query
        )
        lexical_rows = session.execute(
            eligible.where(
                func.to_tsvector("simple", ConversationSegment.text).op("@@")(
                    lexical_query
                )
            )
            .add_columns(lexical_rank.label("lexical_rank"))
            .order_by(desc(lexical_rank), ConversationSegment.ordinal)
            .limit(limit)
        ).all()
        vector_distance = ConversationSegment.embedding.cosine_distance(
            list(query_embedding)
        )
        vector_rows = session.execute(
            eligible.where(ConversationSegment.embedding.is_not(None))
            .add_columns(vector_distance.label("vector_distance"))
            .order_by(vector_distance, ConversationSegment.ordinal)
            .limit(limit)
        ).all()
        candidates: dict[str, RetrievalCandidate] = {}
        for rank, row in enumerate(lexical_rows, start=1):
            segment = row[0]
            candidates.setdefault(segment.content_hash, self._candidate(segment))
            candidates[segment.content_hash].metadata["lexical_rank"] = str(rank)
        for rank, row in enumerate(vector_rows, start=1):
            segment = row[0]
            candidates.setdefault(segment.content_hash, self._candidate(segment))
            candidates[segment.content_hash].metadata["vector_rank"] = str(rank)
        return list(candidates.values())

    @staticmethod
    def _eligible_query(*, workspace_id: UUID, conversation_id: UUID) -> Any:
        """构建排除当前会话和已删除会话的候选查询"""
        return (
            select(ConversationSegment)
            .join(Conversation, Conversation.id == ConversationSegment.conversation_id)
            .where(
                ConversationSegment.workspace_id == workspace_id,
                ConversationSegment.conversation_id != conversation_id,
                ConversationSegment.embedding_status == "ready",
                ConversationSegment.deleted_at.is_(None),
                Conversation.workspace_id == workspace_id,
                Conversation.deleted_at.is_(None),
            )
        )

    @staticmethod
    def _candidate(segment: ConversationSegment) -> RetrievalCandidate:
        """将会话分段转成不具备 Citation 资格的检索候选"""
        return RetrievalCandidate(
            candidate_id=str(segment.id),
            text=segment.text,
            source_kind="conversation_lead",
            content_hash=segment.content_hash,
            embedding=tuple(float(value) for value in (segment.embedding or [])),
            metadata={
                "conversation_id": str(segment.conversation_id),
                "first_message_id": str(segment.first_message_id or ""),
                "last_message_id": str(segment.last_message_id or ""),
            },
        )


class PostgresMemoryRetrievalAdapter:
    """执行 Memory scope 过滤、全文与 exact pgvector 召回"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """召回当前用户和会话可见的 ready 长期记忆"""
        if limit <= 0:
            return []
        eligible = self._eligible_query(
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if session.bind is None or session.bind.dialect.name != "postgresql":
            memories = session.scalars(eligible.order_by(Memory.created_at).limit(limit)).all()
            return [self._candidate(memory) for memory in memories]

        lexical_query = func.websearch_to_tsquery("simple", query)
        lexical_rank = func.ts_rank_cd(
            func.to_tsvector("simple", Memory.content), lexical_query
        )
        lexical_rows = session.execute(
            eligible.where(
                func.to_tsvector("simple", Memory.content).op("@@")(lexical_query)
            )
            .add_columns(lexical_rank.label("lexical_rank"))
            .order_by(desc(lexical_rank), Memory.created_at)
            .limit(limit)
        ).all()
        vector_distance = Memory.embedding.cosine_distance(list(query_embedding))
        vector_rows = session.execute(
            eligible.where(Memory.embedding.is_not(None))
            .add_columns(vector_distance.label("vector_distance"))
            .order_by(vector_distance, Memory.created_at)
            .limit(limit)
        ).all()
        candidates: dict[str, RetrievalCandidate] = {}
        for rank, row in enumerate(lexical_rows, start=1):
            memory = row[0]
            candidates.setdefault(str(memory.id), self._candidate(memory))
            candidates[str(memory.id)].metadata["lexical_rank"] = str(rank)
        for rank, row in enumerate(vector_rows, start=1):
            memory = row[0]
            candidates.setdefault(str(memory.id), self._candidate(memory))
            candidates[str(memory.id)].metadata["vector_rank"] = str(rank)
        return list(candidates.values())

    @staticmethod
    def _eligible_query(
        *, workspace_id: UUID, user_id: UUID, conversation_id: UUID
    ) -> Any:
        """构建状态、有效期和可见范围过滤后的 Memory 查询"""
        now = datetime.now(UTC)
        return select(Memory).where(
            Memory.workspace_id == workspace_id,
            Memory.status == "active",
            Memory.embedding_status == "ready",
            Memory.deleted_at.is_(None),
            or_(Memory.expires_at.is_(None), Memory.expires_at > now),
            or_(
                Memory.scope == "workspace",
                and_(Memory.scope == "user", Memory.user_id == user_id),
                and_(
                    Memory.scope == "conversation",
                    Memory.conversation_id == conversation_id,
                ),
            ),
        )

    @staticmethod
    def _candidate(memory: Memory) -> RetrievalCandidate:
        """将有效 Memory 转成检索候选"""
        return RetrievalCandidate(
            candidate_id=str(memory.id),
            text=memory.content,
            source_kind="memory",
            content_hash=str(memory.id),
            embedding=tuple(float(value) for value in (memory.embedding or [])),
            metadata={"scope": memory.scope},
        )


class PostgresResearchRecordRetrievalAdapter:
    """执行 ResearchRecord 状态过滤、全文与 exact pgvector 召回"""

    def recall(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """召回 Workspace 中 ready 的已核验或争议研究记录"""
        if limit <= 0:
            return []
        eligible = self._eligible_query(workspace_id=workspace_id)
        if session.bind is None or session.bind.dialect.name != "postgresql":
            records = session.scalars(
                eligible.order_by(ResearchRecord.created_at).limit(limit)
            ).all()
            return [self._candidate(record) for record in records]

        lexical_query = func.websearch_to_tsquery("simple", query)
        lexical_rank = func.ts_rank_cd(
            func.to_tsvector("simple", ResearchRecord.claim_text), lexical_query
        )
        lexical_rows = session.execute(
            eligible.where(
                func.to_tsvector("simple", ResearchRecord.claim_text).op("@@")(
                    lexical_query
                )
            )
            .add_columns(lexical_rank.label("lexical_rank"))
            .order_by(desc(lexical_rank), ResearchRecord.created_at)
            .limit(limit)
        ).all()
        vector_distance = ResearchRecord.embedding.cosine_distance(list(query_embedding))
        vector_rows = session.execute(
            eligible.where(ResearchRecord.embedding.is_not(None))
            .add_columns(vector_distance.label("vector_distance"))
            .order_by(vector_distance, ResearchRecord.created_at)
            .limit(limit)
        ).all()
        candidates: dict[str, RetrievalCandidate] = {}
        for rank, row in enumerate(lexical_rows, start=1):
            record = row[0]
            candidates.setdefault(str(record.id), self._candidate(record))
            candidates[str(record.id)].metadata["lexical_rank"] = str(rank)
        for rank, row in enumerate(vector_rows, start=1):
            record = row[0]
            candidates.setdefault(str(record.id), self._candidate(record))
            candidates[str(record.id)].metadata["vector_rank"] = str(rank)
        return list(candidates.values())

    @staticmethod
    def _eligible_query(*, workspace_id: UUID) -> Any:
        """构建空间、索引和生命周期过滤后的 ResearchRecord 查询"""
        return select(ResearchRecord).where(
            ResearchRecord.workspace_id == workspace_id,
            ResearchRecord.status.in_({"verified", "disputed"}),
            ResearchRecord.embedding_status == "ready",
            ResearchRecord.deleted_at.is_(None),
        )

    @staticmethod
    def _candidate(record: ResearchRecord) -> RetrievalCandidate:
        """将有效 ResearchRecord 转成检索候选"""
        return RetrievalCandidate(
            candidate_id=str(record.id),
            text=record.claim_text,
            source_kind="research_record",
            content_hash=record.content_hash,
            embedding=tuple(float(value) for value in (record.embedding or [])),
            metadata={
                "record_key": record.record_key,
                "version": str(record.version),
                "status": record.status,
            },
        )


class LiteLLMRerankGateway:
    """通过 LiteLLM 调用 Qwen 或其他兼容 Provider 的 Rerank Adapter"""

    def __init__(self, *, api_key: str, model: str, api_base: str | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._api_base = api_base

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        """按相关性返回候选下标和分数，Provider 失败时直接抛出异常"""
        from litellm import rerank

        request_args: dict[str, Any] = {
            "model": self._model,
            "api_key": self._api_key,
            "query": query,
            "documents": list(documents),
            "top_n": top_n,
            "return_documents": False,
        }
        if self._api_base is not None:
            request_args["api_base"] = self._api_base
        response = rerank(**request_args)
        return [(int(item["index"]), float(item["relevance_score"])) for item in response.results]


class HybridRetrieval:
    """封装全文、向量、RRF 和 Rerank 的候选排序行为"""

    def __init__(
        self,
        rerank_gateway: RerankGateway,
        *,
        rrf_k: int = 60,
        source_chunk_adapter: SourceChunkRetrievalAdapter | None = None,
        conversation_segment_adapter: ConversationSegmentRetrievalAdapter | None = None,
        memory_adapter: MemoryRetrievalAdapter | None = None,
        research_record_adapter: ResearchRecordRetrievalAdapter | None = None,
    ) -> None:
        self._rerank_gateway = rerank_gateway
        self._rrf_k = rrf_k
        self._source_chunk_adapter = source_chunk_adapter
        self._conversation_segment_adapter = conversation_segment_adapter
        self._memory_adapter = memory_adapter
        self._research_record_adapter = research_record_adapter

    def recall_source_chunks(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """通过持久化 Adapter 召回当前范围内的 SourceChunk"""
        if self._source_chunk_adapter is None:
            return []
        return self._source_chunk_adapter.recall(
            session,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            query=query,
            query_embedding=query_embedding,
            limit=limit,
        )

    def recall_conversation_segments(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """召回其他会话中只用于导航的低信任分段"""
        if self._conversation_segment_adapter is None:
            return []
        return self._conversation_segment_adapter.recall(
            session,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            query=query,
            query_embedding=query_embedding,
            limit=limit,
        )

    def recall_memories(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """召回当前运行可见的有效长期记忆"""
        if self._memory_adapter is None:
            return []
        return self._memory_adapter.recall(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            query=query,
            query_embedding=query_embedding,
            limit=limit,
        )

    def recall_research_records(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        query: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """召回当前 Workspace 可跨会话复用的研究记录"""
        if self._research_record_adapter is None:
            return []
        return self._research_record_adapter.recall(
            session,
            workspace_id=workspace_id,
            query=query,
            query_embedding=query_embedding,
            limit=limit,
        )

    def rank(
        self,
        query: str,
        candidates: Sequence[RetrievalCandidate],
        *,
        limit: int,
        query_embedding: tuple[float, ...] | None = None,
        rerank_limit: int | None = None,
        token_budget: int | None = None,
    ) -> list[RankedCandidate]:
        """融合候选并返回不超过限制的结果"""
        if limit <= 0 or not candidates:
            return []
        lexical: list[tuple[float, int]] = sorted(
            (
                (self._lexical_score(query, candidate.text), index)
                for index, candidate in enumerate(candidates)
            ),
            key=lambda item: (item[0], -item[1]),
            reverse=True,
        )
        lexical = [item for item in lexical if item[0] > 0]
        lexical_metadata = {
            index: int(candidate.metadata["lexical_rank"])
            for index, candidate in enumerate(candidates)
            if candidate.metadata.get("lexical_rank") is not None
        }
        if lexical_metadata:
            lexical_indexes = [
                index for index, _ in sorted(lexical_metadata.items(), key=lambda item: item[1])
            ]
            lexical = [(1.0, index) for index in lexical_indexes]
        vector: list[tuple[float, int]] = sorted(
            (
                (self._cosine_similarity(query_embedding, candidate.embedding), index)
                for index, candidate in enumerate(candidates)
                if query_embedding is not None and candidate.embedding is not None
            ),
            key=lambda item: (item[0], -item[1]),
            reverse=True,
        )
        vector_metadata = {
            index: int(candidate.metadata["vector_rank"])
            for index, candidate in enumerate(candidates)
            if candidate.metadata.get("vector_rank") is not None
        }
        if vector_metadata:
            vector_indexes = [
                index for index, _ in sorted(vector_metadata.items(), key=lambda item: item[1])
            ]
            vector = [(1.0, index) for index in vector_indexes]
        scores: dict[int, float] = {}
        for rank, (_, index) in enumerate(lexical, start=1):
            scores[index] = scores.get(index, 0.0) + 1.0 / (self._rrf_k + rank)
        for rank, (_, index) in enumerate(vector, start=1):
            scores[index] = scores.get(index, 0.0) + 1.0 / (self._rrf_k + rank)
        merged = sorted(scores, key=lambda index: (scores[index], -index), reverse=True)
        if not merged:
            return []
        candidate_limit = min(len(merged), rerank_limit or max(limit, 10))
        rerank_indexes = merged[:candidate_limit]
        reranked = self._rerank_gateway.rerank(
            query,
            [candidates[index].text for index in rerank_indexes],
            min(candidate_limit, max(limit, 1)),
        )
        output: list[RankedCandidate] = []
        for rank, (rerank_index, score) in enumerate(reranked, start=1):
            if rerank_index < 0 or rerank_index >= len(rerank_indexes):
                continue
            candidate = candidates[rerank_indexes[rerank_index]]
            if token_budget is not None and self._token_size(output, candidate) > token_budget:
                break
            output.append(RankedCandidate(candidate=candidate, score=score, rank=rank))
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _lexical_score(query: str, text: str) -> int:
        """计算中英文词项和中文二元词的匹配分数"""
        normalized_query = query.casefold()
        normalized_text = text.casefold()
        words = re.findall(r"[a-z0-9_-]{2,}", normalized_query)
        sequences = re.findall(r"[\u3400-\u9fff]+", normalized_query)
        bigrams = [
            sequence[index : index + 2]
            for sequence in sequences
            for index in range(len(sequence) - 1)
        ]
        return sum(normalized_text.count(term) for term in [*words, *bigrams])

    @staticmethod
    def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        """计算两个向量的余弦相似度"""
        if len(left) != len(right) or not left:
            return 0.0
        denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
            sum(value * value for value in right)
        )
        return (
            sum(a * b for a, b in zip(left, right, strict=True)) / denominator
            if denominator
            else 0.0
        )

    @staticmethod
    def _token_size(output: Sequence[RankedCandidate], candidate: RetrievalCandidate) -> int:
        """估算已选候选和新候选的 token 占用"""
        return sum(len(item.candidate.text) for item in output) + len(candidate.text)
