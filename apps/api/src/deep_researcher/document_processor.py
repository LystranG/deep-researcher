import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from docx import Document as DocxDocument
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import (
    Attachment,
    ConversationSegment,
    Memory,
    Message,
    ResearchRecord,
    SourceChunk,
)
from deep_researcher.retrieval import EmbeddingGateway
from deep_researcher.storage import LocalObjectStore

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".sql",
    ".yaml",
    ".yml",
}


class DocumentIndexingError(RuntimeError):
    """表示文档解析完成后的 embedding 索引失败"""


class DocumentProcessor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_store: LocalObjectStore,
        embedding_gateway: EmbeddingGateway | None = None,
    ) -> None:
        """初始化异步文档解析与可选 embedding 索引处理器"""
        self._session_factory = session_factory
        self._object_store = object_store
        self._embedding_gateway = embedding_gateway

    def process(self, attachment_id: UUID) -> None:
        """解析附件并在配置 Provider 时完成切块 embedding"""
        try:
            with self._session_factory() as session:
                attachment = session.get(Attachment, attachment_id)
                if attachment is None:
                    return
                pages = self._extract_pages(
                    self._object_store.path_for(attachment.storage_key),
                    attachment.filename,
                    attachment.mime_type,
                )
                chunks: list[SourceChunk] = []
                ordinal = 1
                for page_number, text in pages:
                    for chunk_data in self._chunks(text, page_number=page_number):
                        chunks.append(
                            SourceChunk(
                                workspace_id=attachment.workspace_id,
                                conversation_id=attachment.conversation_id,
                                attachment_id=attachment.id,
                                ordinal=ordinal,
                                text=chunk_data[0],
                                page_number=page_number,
                                start_offset=chunk_data[1],
                                end_offset=chunk_data[2],
                                content_hash=hashlib.sha256(chunk_data[0].encode()).hexdigest(),
                            )
                        )
                        ordinal += 1
                if self._embedding_gateway is not None and chunks:
                    try:
                        embeddings = self._embedding_gateway.embed_documents(
                            [chunk.text for chunk in chunks]
                        )
                        if len(embeddings) != len(chunks):
                            raise ValueError("embedding 返回数量与文档切块数量不一致")
                        dimensions = len(embeddings[0])
                        if dimensions <= 0 or any(
                            len(embedding) != dimensions for embedding in embeddings
                        ):
                            raise ValueError("embedding 维度无效或不一致")
                        indexed_at = datetime.now(UTC)
                        for source_chunk, embedding in zip(chunks, embeddings, strict=True):
                            source_chunk.embedding = list(embedding)
                            source_chunk.embedding_model = self._embedding_gateway.model_name
                            source_chunk.embedding_dimensions = dimensions
                            source_chunk.embedding_status = "ready"
                            source_chunk.embedding_error = None
                            source_chunk.indexed_at = indexed_at
                    except Exception as exc:
                        raise DocumentIndexingError("文档 embedding 索引失败") from exc
                session.add_all(chunks)
                attachment.status = "ready"
                attachment.failure_reason = None
                attachment.processed_at = datetime.now(UTC)
                session.commit()
        except Exception as exc:
            with self._session_factory.begin() as session:
                attachment = session.get(Attachment, attachment_id)
                if attachment is not None:
                    attachment.status = "failed"
                    attachment.failure_reason = self._safe_failure(exc)
                    attachment.processed_at = datetime.now(UTC)

    def _extract_pages(
        self, path: Path, filename: str, mime_type: str
    ) -> list[tuple[int | None, str]]:
        suffix = Path(filename).suffix.lower()
        if suffix in TEXT_EXTENSIONS or mime_type.startswith("text/"):
            raw = path.read_bytes()
            if b"\x00" in raw:
                raise ValueError("文件包含二进制内容，无法按文本解析")
            return [(None, raw.decode("utf-8"))]
        if suffix == ".pdf" or mime_type == "application/pdf":
            reader = PdfReader(path)
            return [
                (index, page.extract_text() or "") for index, page in enumerate(reader.pages, 1)
            ]
        if suffix == ".docx" or mime_type.endswith("wordprocessingml.document"):
            document = DocxDocument(str(path))
            return [(None, "\n".join(paragraph.text for paragraph in document.paragraphs))]
        if mime_type in {"image/png", "image/jpeg", "image/webp"}:
            with Image.open(path) as image:
                image.verify()
            return []
        raise ValueError("暂不支持该文件格式")

    @staticmethod
    def _chunks(text: str, *, page_number: int | None) -> list[tuple[str, int, int]]:
        del page_number
        normalized = text.strip()
        if not normalized:
            return []
        return [
            (normalized[start : start + 2000], start, min(start + 2000, len(normalized)))
            for start in range(0, len(normalized), 2000)
        ]

    @staticmethod
    def _safe_failure(exc: Exception) -> str:
        if isinstance(exc, (DocumentIndexingError, ValueError, UnicodeDecodeError)):
            return str(exc)[:500]
        return "文件解析失败，请确认文件未损坏且格式正确"


class ConversationSegmentProcessor:
    """异步维护历史会话的确定性分段和 embedding 索引"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedding_gateway: EmbeddingGateway | None = None,
    ) -> None:
        """初始化会话分段处理器"""
        self._session_factory = session_factory
        self._embedding_gateway = embedding_gateway

    def process(self, conversation_id: UUID) -> None:
        """按消息顺序重建有界历史分段并记录索引状态"""
        with self._session_factory.begin() as session:
            messages = session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.created_at, Message.id)
            ).all()
            if not messages:
                return
            conversation = messages[0].conversation_id
            segment_texts: list[tuple[str, UUID, UUID]] = []
            current: list[Message] = []
            current_size = 0
            for message in messages:
                text = f"{message.role}: {message.content}".strip()
                if current and current_size + len(text) + 1 > 2000:
                    segment_texts.append(
                        (
                            "\n".join(
                                f"{item.role}: {item.content}" for item in current
                            ),
                            current[0].id,
                            current[-1].id,
                        )
                    )
                    current = []
                    current_size = 0
                current.append(message)
                current_size += len(text) + 1
            if current:
                segment_texts.append(
                    (
                        "\n".join(f"{item.role}: {item.content}" for item in current),
                        current[0].id,
                        current[-1].id,
                    )
                )
            existing = {
                segment.ordinal: segment
                for segment in session.scalars(
                    select(ConversationSegment).where(
                        ConversationSegment.conversation_id == conversation
                    )
                ).all()
            }
            embeddings: list[tuple[float, ...]] = []
            if self._embedding_gateway is not None:
                try:
                    embeddings = self._embedding_gateway.embed_documents(
                        [text for text, _, _ in segment_texts]
                    )
                    if len(embeddings) != len(segment_texts):
                        raise ValueError("会话分段 embedding 返回数量不一致")
                    dimensions = len(embeddings[0])
                    if dimensions <= 0 or any(len(item) != dimensions for item in embeddings):
                        raise ValueError("会话分段 embedding 维度无效")
                except Exception as exc:
                    failure = (
                        str(exc)[:500]
                        if isinstance(exc, ValueError)
                        else "会话分段 embedding 失败"
                    )
                    embeddings = []
                    for failed_segment in existing.values():
                        failed_segment.embedding_status = "failed"
                        failed_segment.embedding_error = failure
                    if not existing:
                        for ordinal in range(1, len(segment_texts) + 1):
                            failed_segment = ConversationSegment(
                                workspace_id=messages[0].workspace_id,
                                conversation_id=conversation,
                                ordinal=ordinal,
                                text=segment_texts[ordinal - 1][0],
                                content_hash=hashlib.sha256(
                                    segment_texts[ordinal - 1][0].encode()
                                ).hexdigest(),
                                embedding_status="failed",
                                embedding_error=failure,
                            )
                            session.add(failed_segment)
                    return
            indexed_at = datetime.now(UTC)
            for ordinal, (text, first_id, last_id) in enumerate(segment_texts, start=1):
                current_segment = existing.get(ordinal)
                if current_segment is None:
                    current_segment = ConversationSegment(
                        workspace_id=messages[0].workspace_id,
                        conversation_id=conversation,
                        ordinal=ordinal,
                    )
                    session.add(current_segment)
                current_segment.first_message_id = first_id
                current_segment.last_message_id = last_id
                current_segment.text = text
                current_segment.content_hash = hashlib.sha256(text.encode()).hexdigest()
                current_segment.deleted_at = None
                if self._embedding_gateway is None:
                    continue
                embedding = embeddings[ordinal - 1]
                current_segment.embedding = list(embedding)
                current_segment.embedding_model = self._embedding_gateway.model_name
                current_segment.embedding_dimensions = len(embedding)
                current_segment.embedding_status = "ready"
                current_segment.embedding_error = None
                current_segment.indexed_at = indexed_at
            for ordinal, stale_segment in existing.items():
                if ordinal > len(segment_texts):
                    stale_segment.deleted_at = indexed_at


class MemoryIndexer:
    """异步为有效长期记忆生成可检索 embedding"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedding_gateway: EmbeddingGateway | None = None,
    ) -> None:
        """初始化 Memory 索引处理器"""
        self._session_factory = session_factory
        self._embedding_gateway = embedding_gateway

    def process_workspace(self, workspace_id: UUID) -> None:
        """索引指定 Workspace 中尚未完成的有效 Memory"""
        if self._embedding_gateway is None:
            return
        with self._session_factory.begin() as session:
            memories = session.scalars(
                select(Memory).where(
                    Memory.workspace_id == workspace_id,
                    Memory.status == "active",
                    Memory.deleted_at.is_(None),
                    Memory.embedding_status.in_({"pending", "failed"}),
                )
            ).all()
            if not memories:
                return
            try:
                embeddings = self._embedding_gateway.embed_documents(
                    [memory.content for memory in memories]
                )
                if len(embeddings) != len(memories):
                    raise ValueError("Memory embedding 返回数量不一致")
                dimensions = len(embeddings[0])
                if dimensions <= 0 or any(len(item) != dimensions for item in embeddings):
                    raise ValueError("Memory embedding 维度无效")
            except Exception as exc:
                failure = str(exc)[:500] if isinstance(exc, ValueError) else "Memory embedding 失败"
                for memory in memories:
                    memory.embedding_status = "failed"
                    memory.embedding_error = failure
                return
            indexed_at = datetime.now(UTC)
            for memory, embedding in zip(memories, embeddings, strict=True):
                memory.embedding = list(embedding)
                memory.embedding_model = self._embedding_gateway.model_name
                memory.embedding_dimensions = dimensions
                memory.embedding_status = "ready"
                memory.embedding_error = None
                memory.indexed_at = indexed_at


class ResearchRecordIndexer:
    """异步为可复用 ResearchRecord 生成检索 embedding"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedding_gateway: EmbeddingGateway | None = None,
    ) -> None:
        """初始化 ResearchRecord 索引处理器"""
        self._session_factory = session_factory
        self._embedding_gateway = embedding_gateway

    def process(self, record_id: UUID) -> None:
        """索引指定的未删除且可检索 ResearchRecord"""
        if self._embedding_gateway is None:
            return
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(ResearchRecord).where(
                    ResearchRecord.id == record_id,
                    ResearchRecord.status.in_({"verified", "disputed"}),
                    ResearchRecord.deleted_at.is_(None),
                    ResearchRecord.embedding_status.in_({"pending", "failed"}),
                )
            )
            if record is None:
                return
            try:
                embeddings = self._embedding_gateway.embed_documents([record.claim_text])
                if len(embeddings) != 1:
                    raise ValueError("ResearchRecord embedding 返回数量不一致")
                embedding = embeddings[0]
                if not embedding:
                    raise ValueError("ResearchRecord embedding 维度无效")
            except Exception as exc:
                record.embedding_status = "failed"
                record.embedding_error = (
                    str(exc)[:500]
                    if isinstance(exc, ValueError)
                    else "ResearchRecord embedding 失败"
                )
                return
            record.embedding = list(embedding)
            record.embedding_model = self._embedding_gateway.model_name
            record.embedding_dimensions = len(embedding)
            record.embedding_status = "ready"
            record.embedding_error = None
            record.indexed_at = datetime.now(UTC)
