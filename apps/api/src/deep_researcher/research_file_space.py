"""Minimal durable Research File Space for sources, task work, and artifacts."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import (
    Attachment,
    Document,
    DocumentVersion,
    ResearchArtifactRevision,
    ResearchRun,
    ResearchSourceMount,
    ResearchTask,
    ResearchWorkRevision,
    RunEvent,
)

if TYPE_CHECKING:
    from deep_researcher.storage import LocalObjectStore
    from deep_researcher.task_runtime import (
        TaskClaim,
        TaskObservationResult,
        TaskToolCall,
        ToolDefinition,
    )


@dataclass(frozen=True)
class ResearchFileRef:
    """Stable identity for an immutable Research File Space revision."""

    kind: str
    id: UUID
    revision: str

    def __post_init__(self) -> None:
        if self.kind not in {"source", "work", "artifact"}:
            raise ValueError(f"invalid research file kind: {self.kind}")
        if not self.revision:
            raise ValueError("research file revision is required")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": str(self.id), "revision": self.revision}

    def __str__(self) -> str:
        return f"research-file://{self.kind}/{self.id}/{self.revision}"

    @classmethod
    def parse(cls, value: ResearchFileRef | Mapping[str, object] | str) -> ResearchFileRef:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                kind=str(value["kind"]),
                id=UUID(str(value["id"])),
                revision=str(value["revision"]),
            )
        if not isinstance(value, str):
            raise ValueError("invalid research file reference")
        prefix = "research-file://"
        if not value.startswith(prefix):
            raise ValueError("invalid research file reference")
        kind, identifier, revision = value[len(prefix) :].split("/", 2)
        if not revision or "/" in revision:
            raise ValueError("invalid research file revision")
        return cls(kind=kind, id=UUID(identifier), revision=revision)


@dataclass(frozen=True)
class ResearchFileSnapshot:
    ref: ResearchFileRef
    name: str
    content_hash: str
    size_bytes: int
    media_type: str
    status: str
    content: str | None = None
    parent_ref: ResearchFileRef | None = None
    source_ref: ResearchFileRef | None = None
    failure_reason: str | None = None

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "ref": self.ref.as_dict(),
            "name": self.name,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "status": self.status,
            "failure_reason": self.failure_reason,
        }
        if self.parent_ref is not None:
            result["parent_ref"] = self.parent_ref.as_dict()
        if self.source_ref is not None:
            result["source_ref"] = self.source_ref.as_dict()
        if include_content:
            result["content"] = self.content
        return result


class ResearchFileError(Exception):
    code = "research_file_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ResearchFileNotFound(ResearchFileError):
    code = "file_not_found"


class ResearchFileAccessError(ResearchFileError):
    code = "file_access_denied"


class ResearchFileConflict(ResearchFileError):
    code = "revision_conflict"

    def __init__(self, message: str, *, current_ref: ResearchFileRef | None) -> None:
        super().__init__(message)
        self.current_ref = current_ref


def normalize_research_file_name(name: str) -> str:
    normalized = unicodedata.normalize("NFC", name.strip()).replace("\\", "/")
    if not normalized or "\x00" in normalized or normalized.startswith("/"):
        raise ValueError("research file name must be a non-empty relative path")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("research file name contains an invalid path segment")
    value = str(path)
    if len(value) > 1000:
        raise ValueError("research file name is too long")
    return value


def freeze_run_sources(
    session: Session,
    run: ResearchRun,
    attachments: Sequence[Attachment] = (),
) -> tuple[ResearchSourceMount, ...]:
    """Freeze attachment and current Workspace Document revisions in the Run transaction."""
    existing = session.scalars(
        select(ResearchSourceMount).where(ResearchSourceMount.run_id == run.id)
    ).all()
    if existing:
        return tuple(existing)

    candidates: list[tuple[str, UUID, str, str, str, int, str | None]] = []
    for attachment in attachments:
        candidates.append(
            (
                "attachment",
                attachment.id,
                "1",
                attachment.filename,
                attachment.mime_type,
                attachment.size_bytes,
                attachment.storage_key,
            )
        )
    documents = session.execute(
        select(Document, DocumentVersion)
        .join(
            DocumentVersion,
            (DocumentVersion.document_id == Document.id)
            & (DocumentVersion.version == Document.current_version),
        )
        .where(
            Document.workspace_id == run.workspace_id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at, Document.id)
    ).all()
    for document, version in documents:
        candidates.append(
            (
                "document",
                document.id,
                str(version.version),
                document.filename,
                version.mime_type,
                version.size_bytes,
                version.storage_key,
            )
        )

    used_names: set[str] = set()
    mounts: list[ResearchSourceMount] = []
    for source_type, entity_id, revision, name, media_type, size, storage_key in candidates:
        normalized_name = _unique_name(name, entity_id, used_names)
        content_hash = next(
            (
                item.sha256
                for item in attachments
                if source_type == "attachment" and item.id == entity_id
            ),
            "",
        )
        if source_type == "document":
            content_hash = next(
                version.sha256
                for document, version in documents
                if document.id == entity_id and str(version.version) == revision
            )
        mount = ResearchSourceMount(
            workspace_id=run.workspace_id,
            run_id=run.id,
            normalized_name=normalized_name,
            source_type=source_type,
            source_entity_id=entity_id,
            source_revision=revision,
            media_type=media_type,
            content_hash=content_hash,
            size_bytes=size,
            storage_key=storage_key,
        )
        session.add(mount)
        mounts.append(mount)
    session.flush()
    return tuple(mounts)


class ResearchFileStore:
    """Persistence boundary for immutable Research File Space facts."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_store: LocalObjectStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store

    def list_run(self, workspace_id: UUID, run_id: UUID) -> dict[str, list[dict[str, object]]]:
        with self._session_factory() as session:
            self._require_run(session, workspace_id, run_id)
            sources = session.scalars(
                select(ResearchSourceMount)
                .where(
                    ResearchSourceMount.workspace_id == workspace_id,
                    ResearchSourceMount.run_id == run_id,
                )
                .order_by(ResearchSourceMount.normalized_name)
            ).all()
            work = session.scalars(
                select(ResearchWorkRevision)
                .where(
                    ResearchWorkRevision.workspace_id == workspace_id,
                    ResearchWorkRevision.run_id == run_id,
                )
                .order_by(
                    ResearchWorkRevision.normalized_name,
                    ResearchWorkRevision.revision,
                )
            ).all()
            artifacts = session.scalars(
                select(ResearchArtifactRevision)
                .where(
                    ResearchArtifactRevision.workspace_id == workspace_id,
                    ResearchArtifactRevision.run_id == run_id,
                )
                .order_by(
                    ResearchArtifactRevision.normalized_name,
                    ResearchArtifactRevision.revision,
                )
            ).all()
            return {
                "sources": [self._source_snapshot(item).as_dict() for item in sources],
                "work": [self._work_snapshot(item, session).as_dict() for item in work],
                "artifacts": [
                    self._artifact_snapshot(item, session).as_dict() for item in artifacts
                ],
            }

    def write(
        self,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID,
        name: str,
        content: str,
        idempotency_key: str,
        expected_revision: ResearchFileRef | Mapping[str, object] | str | None = None,
        media_type: str | None = None,
    ) -> ResearchFileSnapshot:
        normalized_name = normalize_research_file_name(name)
        expected = (
            ResearchFileRef.parse(expected_revision) if expected_revision is not None else None
        )
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("a bounded idempotency key is required")
        encoded = content.encode("utf-8")
        if len(encoded) > 5_000_000:
            raise ValueError("research work file exceeds the 5 MB limit")

        with self._session_factory() as session:
            self._require_task(session, workspace_id, run_id, task_id)
            replay = session.scalar(
                select(ResearchWorkRevision).where(
                    ResearchWorkRevision.task_id == task_id,
                    ResearchWorkRevision.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                return self._work_snapshot(replay, session)
            current = session.scalar(
                select(ResearchWorkRevision)
                .where(
                    ResearchWorkRevision.task_id == task_id,
                    ResearchWorkRevision.normalized_name == normalized_name,
                )
                .order_by(ResearchWorkRevision.revision.desc())
            )
            current_ref = self._work_ref(current) if current is not None else None
            if current is None and expected is not None:
                raise ResearchFileConflict(
                    "expected revision does not exist", current_ref=None
                )
            if current is not None and expected != current_ref:
                raise ResearchFileConflict(
                    "work file has a newer committed revision",
                    current_ref=current_ref,
                )
            revision = ResearchWorkRevision(
                file_id=current.file_id if current is not None else uuid4(),
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                normalized_name=normalized_name,
                revision=1 if current is None else current.revision + 1,
                parent_revision_id=current.id if current is not None else None,
                content=content,
                content_hash=hashlib.sha256(encoded).hexdigest(),
                size_bytes=len(encoded),
                media_type=media_type
                or mimetypes.guess_type(normalized_name)[0]
                or "text/plain",
                idempotency_key=idempotency_key,
            )
            try:
                session.add(revision)
                session.flush()
                self._append_event(
                    session,
                    run_id,
                    "file_revision_committed",
                    f"file-revision:{revision.file_id}:{revision.revision}",
                    self._work_snapshot(revision, session).as_dict(),
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                replay = session.scalar(
                    select(ResearchWorkRevision).where(
                        ResearchWorkRevision.task_id == task_id,
                        ResearchWorkRevision.idempotency_key == idempotency_key,
                    )
                )
                if replay is not None:
                    return self._work_snapshot(replay, session)
                current = session.scalar(
                    select(ResearchWorkRevision)
                    .where(
                        ResearchWorkRevision.task_id == task_id,
                        ResearchWorkRevision.normalized_name == normalized_name,
                    )
                    .order_by(ResearchWorkRevision.revision.desc())
                )
                raise ResearchFileConflict(
                    "concurrent work file update lost compare-and-swap",
                    current_ref=self._work_ref(current) if current is not None else None,
                ) from exc
            return self._work_snapshot(revision, session)

    def read(
        self,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID,
        ref: ResearchFileRef | Mapping[str, object] | str,
        shared_refs: Iterable[ResearchFileRef | Mapping[str, object] | str] = (),
    ) -> ResearchFileSnapshot:
        parsed = ResearchFileRef.parse(ref)
        grants = {ResearchFileRef.parse(item) for item in shared_refs}
        with self._session_factory() as session:
            self._require_task(session, workspace_id, run_id, task_id)
            if parsed.kind == "source":
                mount = session.get(ResearchSourceMount, parsed.id)
                if (
                    mount is None
                    or mount.workspace_id != workspace_id
                    or mount.run_id != run_id
                    or mount.source_revision != parsed.revision
                ):
                    raise ResearchFileNotFound("source revision is not mounted in this Run")
                snapshot = self._source_snapshot(mount)
                return ResearchFileSnapshot(
                    **{**snapshot.__dict__, "content": self._read_source_content(mount)}
                )
            if parsed.kind == "work":
                revision = self._work_by_ref(session, parsed, workspace_id, run_id)
                if revision.task_id != task_id and parsed not in grants:
                    raise ResearchFileAccessError(
                        "cross-Task work requires an explicit read-only revision ref"
                    )
                return self._work_snapshot(revision, session)
            artifact = session.get(ResearchArtifactRevision, parsed.id)
            if (
                artifact is None
                or artifact.workspace_id != workspace_id
                or artifact.run_id != run_id
                or str(artifact.revision) != parsed.revision
            ):
                raise ResearchFileNotFound("artifact revision is not available")
            return self._artifact_snapshot(artifact, session, include_content=True)

    def publish(
        self,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID,
        work_ref: ResearchFileRef | Mapping[str, object] | str,
        name: str,
        idempotency_key: str,
    ) -> ResearchFileSnapshot:
        parsed = ResearchFileRef.parse(work_ref)
        normalized_name = normalize_research_file_name(name)
        if parsed.kind != "work":
            raise ResearchFileAccessError("only committed work revisions can be published")
        with self._session_factory() as session:
            self._require_task(session, workspace_id, run_id, task_id)
            replay = session.scalar(
                select(ResearchArtifactRevision).where(
                    ResearchArtifactRevision.run_id == run_id,
                    ResearchArtifactRevision.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                return self._artifact_snapshot(replay, session)
            work = self._work_by_ref(session, parsed, workspace_id, run_id)
            if work.task_id != task_id:
                raise ResearchFileAccessError("a Task can publish only its own committed work")
            if work.status != "committed":
                raise ResearchFileAccessError("work revision is not committed")
            current = session.scalar(
                select(ResearchArtifactRevision)
                .where(
                    ResearchArtifactRevision.run_id == run_id,
                    ResearchArtifactRevision.normalized_name == normalized_name,
                )
                .order_by(ResearchArtifactRevision.revision.desc())
            )
            artifact = ResearchArtifactRevision(
                artifact_id=current.artifact_id if current is not None else uuid4(),
                workspace_id=workspace_id,
                run_id=run_id,
                published_by_task_id=task_id,
                work_revision_id=work.id,
                normalized_name=normalized_name,
                revision=1 if current is None else current.revision + 1,
                content_hash=work.content_hash,
                size_bytes=work.size_bytes,
                media_type=work.media_type,
                idempotency_key=idempotency_key,
            )
            try:
                session.add(artifact)
                session.flush()
                self._append_event(
                    session,
                    run_id,
                    "artifact_published",
                    f"artifact:{artifact.id}:{artifact.revision}",
                    self._artifact_snapshot(artifact, session).as_dict(),
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                replay = session.scalar(
                    select(ResearchArtifactRevision).where(
                        ResearchArtifactRevision.run_id == run_id,
                        ResearchArtifactRevision.idempotency_key == idempotency_key,
                    )
                )
                if replay is None:
                    raise
                return self._artifact_snapshot(replay, session)
            return self._artifact_snapshot(artifact, session)

    def ingest_artifact(
        self,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID,
        name: str,
        content: str,
        idempotency_key: str,
    ) -> ResearchFileSnapshot:
        """Commit Sandbox text output as work, then publish its artifact ref."""
        work = self.write(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            name=name,
            content=content,
            idempotency_key=f"{idempotency_key}:work",
        )
        return self.publish(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            work_ref=work.ref,
            name=name,
            idempotency_key=f"{idempotency_key}:artifact",
        )

    @staticmethod
    def _require_run(session: Session, workspace_id: UUID, run_id: UUID) -> ResearchRun:
        run = session.get(ResearchRun, run_id)
        if run is None or run.workspace_id != workspace_id:
            raise ResearchFileNotFound("Research Run is not available")
        return run

    @classmethod
    def _require_task(
        cls, session: Session, workspace_id: UUID, run_id: UUID, task_id: UUID
    ) -> ResearchTask:
        cls._require_run(session, workspace_id, run_id)
        task = session.get(ResearchTask, task_id)
        if task is None or task.workspace_id != workspace_id or task.run_id != run_id:
            raise ResearchFileNotFound("Research Task is not available")
        return task

    @staticmethod
    def _source_snapshot(row: ResearchSourceMount) -> ResearchFileSnapshot:
        return ResearchFileSnapshot(
            ref=ResearchFileRef("source", row.id, row.source_revision),
            name=row.normalized_name,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            status="frozen",
        )

    @staticmethod
    def _work_ref(row: ResearchWorkRevision) -> ResearchFileRef:
        return ResearchFileRef("work", row.file_id, str(row.revision))

    @classmethod
    def _work_snapshot(
        cls, row: ResearchWorkRevision, session: Session
    ) -> ResearchFileSnapshot:
        parent = (
            session.get(ResearchWorkRevision, row.parent_revision_id)
            if row.parent_revision_id is not None
            else None
        )
        return ResearchFileSnapshot(
            ref=cls._work_ref(row),
            name=row.normalized_name,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            status=row.status,
            content=row.content,
            parent_ref=cls._work_ref(parent) if parent is not None else None,
            failure_reason=row.failure_reason,
        )

    @classmethod
    def _artifact_snapshot(
        cls,
        row: ResearchArtifactRevision,
        session: Session,
        *,
        include_content: bool = False,
    ) -> ResearchFileSnapshot:
        work = session.get(ResearchWorkRevision, row.work_revision_id)
        if work is None:
            raise ResearchFileNotFound("published work revision is unavailable")
        return ResearchFileSnapshot(
            ref=ResearchFileRef("artifact", row.id, str(row.revision)),
            name=row.normalized_name,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            status=row.status,
            content=work.content if include_content else None,
            source_ref=cls._work_ref(work),
            failure_reason=row.failure_reason,
        )

    @staticmethod
    def _work_by_ref(
        session: Session,
        ref: ResearchFileRef,
        workspace_id: UUID,
        run_id: UUID,
    ) -> ResearchWorkRevision:
        revision = session.scalar(
            select(ResearchWorkRevision).where(
                ResearchWorkRevision.file_id == ref.id,
                ResearchWorkRevision.revision == int(ref.revision),
                ResearchWorkRevision.workspace_id == workspace_id,
                ResearchWorkRevision.run_id == run_id,
            )
        )
        if revision is None:
            raise ResearchFileNotFound("work revision is not available")
        return revision

    def _read_source_content(self, row: ResearchSourceMount) -> str | None:
        if row.text_content is not None:
            return row.text_content
        if self._object_store is None or row.storage_key is None:
            return None
        return self._object_store.read_bytes(row.storage_key).decode("utf-8")

    @staticmethod
    def _append_event(
        session: Session,
        run_id: UUID,
        event_type: str,
        event_key: str,
        payload: dict[str, object],
    ) -> None:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise ResearchFileNotFound("Research Run is not available")
        session.add(
            RunEvent(
                workspace_id=run.workspace_id,
                run_id=run.id,
                seq=run.next_event_seq,
                type=event_type,
                event_key=event_key,
                payload=payload,
            )
        )
        run.next_event_seq += 1


class ResearchFileToolAdapter:
    """Task Tool Adapter backed only by the Research File Store."""

    def __init__(self, store: ResearchFileStore) -> None:
        self._store = store

    def execute(self, claim: TaskClaim, call: TaskToolCall) -> TaskObservationResult:
        from deep_researcher.task_runtime import TaskObservationResult

        arguments = call.arguments
        idempotency_key = call.logical_call_ref or call.parameters_hash or (
            f"file-tool:{claim.task_id}:{call.tool_name}"
        )
        if call.tool_name == "file_write":
            result = self._store.write(
                workspace_id=self._workspace_id(claim),
                run_id=claim.run_id,
                task_id=claim.task_id,
                name=str(arguments["name"]),
                content=str(arguments["content"]),
                expected_revision=cast(
                    ResearchFileRef | Mapping[str, object] | str | None,
                    arguments.get("expected_revision"),
                ),
                idempotency_key=idempotency_key,
            )
        elif call.tool_name == "file_publish":
            result = self._store.publish(
                workspace_id=self._workspace_id(claim),
                run_id=claim.run_id,
                task_id=claim.task_id,
                work_ref=cast(
                    ResearchFileRef | Mapping[str, object] | str,
                    arguments["work_ref"],
                ),
                name=str(arguments["name"]),
                idempotency_key=idempotency_key,
            )
        elif call.tool_name in {"file_read", "file_stat"}:
            result = self._store.read(
                workspace_id=self._workspace_id(claim),
                run_id=claim.run_id,
                task_id=claim.task_id,
                ref=cast(
                    ResearchFileRef | Mapping[str, object] | str,
                    arguments["ref"],
                ),
                shared_refs=cast(
                    Iterable[ResearchFileRef | Mapping[str, object] | str],
                    arguments.get("shared_refs", ()),
                ),
            )
        else:
            listing = self._store.list_run(self._workspace_id(claim), claim.run_id)
            summary = json.dumps(listing, ensure_ascii=False, separators=(",", ":"))[:2000]
            return TaskObservationResult(
                result_reference=f"research-file-list://{claim.run_id}",
                summary=summary,
            )
        return TaskObservationResult(
            result_reference=str(result.ref),
            evidence_refs=(str(result.ref),),
            file_refs=(result.ref.as_dict(),),
            evidence_gain=call.tool_name in {"file_write", "file_publish"},
            summary=json.dumps(
                result.as_dict(include_content=call.tool_name == "file_read"),
                ensure_ascii=False,
            )[:2000],
        )

    def _workspace_id(self, claim: TaskClaim) -> UUID:
        with self._store._session_factory() as session:
            task = session.get(ResearchTask, claim.task_id)
            if task is None or task.run_id != claim.run_id:
                raise ResearchFileAccessError("Task claim is not available")
            return task.workspace_id


def research_file_tool_definitions(store: ResearchFileStore) -> tuple[ToolDefinition, ...]:
    """Return Tool Registry definitions for the minimal File Space round trip."""
    from deep_researcher.task_runtime import ToolDefinition

    adapter = ResearchFileToolAdapter(store)
    ref_schema: dict[str, object] = {
        "type": "object",
        "required": ["kind", "id", "revision"],
        "properties": {
            "kind": {"type": "string"},
            "id": {"type": "string"},
            "revision": {"type": "string"},
        },
        "additionalProperties": False,
    }
    return (
        ToolDefinition(
            name="file_list",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            result_contract="bounded file revision list",
            handler=adapter,
        ),
        ToolDefinition(
            name="file_stat",
            input_schema={
                "type": "object",
                "required": ["ref"],
                "properties": {"ref": ref_schema},
                "additionalProperties": False,
            },
            result_contract="stable file metadata",
            handler=adapter,
        ),
        ToolDefinition(
            name="file_read",
            input_schema={
                "type": "object",
                "required": ["ref"],
                "properties": {"ref": ref_schema, "shared_refs": {"type": "array"}},
                "additionalProperties": False,
            },
            result_contract="bounded immutable file content",
            handler=adapter,
        ),
        ToolDefinition(
            name="file_write",
            input_schema={
                "type": "object",
                "required": ["name", "content"],
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "expected_revision": ref_schema,
                },
                "additionalProperties": False,
            },
            result_contract="committed work revision",
            handler=adapter,
        ),
        ToolDefinition(
            name="file_publish",
            input_schema={
                "type": "object",
                "required": ["work_ref", "name"],
                "properties": {"work_ref": ref_schema, "name": {"type": "string"}},
                "additionalProperties": False,
            },
            result_contract="published artifact revision",
            handler=adapter,
        ),
    )


def _unique_name(name: str, identifier: UUID, used_names: set[str]) -> str:
    try:
        normalized = normalize_research_file_name(PurePosixPath(name).name)
    except ValueError:
        normalized = f"source-{identifier}"
    if normalized in used_names:
        path = PurePosixPath(normalized)
        normalized = f"{path.stem}-{str(identifier)[:8]}{path.suffix}"
    used_names.add(normalized)
    return normalized
