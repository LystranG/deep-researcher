import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from docx import Document as DocxDocument
from PIL import Image
from pypdf import PdfReader
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import Attachment, SourceChunk
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


class DocumentProcessor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_store: LocalObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store

    def process(self, attachment_id: UUID) -> None:
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
                ordinal = 1
                for page_number, text in pages:
                    for chunk in self._chunks(text, page_number=page_number):
                        session.add(
                            SourceChunk(
                                workspace_id=attachment.workspace_id,
                                conversation_id=attachment.conversation_id,
                                attachment_id=attachment.id,
                                ordinal=ordinal,
                                text=chunk[0],
                                page_number=page_number,
                                start_offset=chunk[1],
                                end_offset=chunk[2],
                                content_hash=hashlib.sha256(chunk[0].encode()).hexdigest(),
                            )
                        )
                        ordinal += 1
                attachment.status = "ready"
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
        if isinstance(exc, (ValueError, UnicodeDecodeError)):
            return str(exc)[:500]
        return "文件解析失败，请确认文件未损坏且格式正确"
