from _thread import LockType
from dataclasses import dataclass
from threading import Lock
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import ResearchRun, RunEvent


class RunEventRejectedError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistedRunEvent:
    seq: int
    type: str
    payload: dict[str, object]


class RunEventLog:
    """以数据库行锁为跨进程边界，为每个研究运行分配连续事件序号。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        process_lock: LockType | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._process_lock = process_lock or Lock()

    def append(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict[str, object],
        *,
        event_key: str | None = None,
        lease_owner: str | None = None,
    ) -> int:
        with self._process_lock, self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            if event_key is not None:
                existing = session.scalar(
                    select(RunEvent).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_key == event_key,
                    )
                )
                if existing is not None:
                    return existing.seq
            if (
                run is None
                or run.status in {"cancelled", "failed", "completed"}
                or run.cancel_requested_at is not None
                or (lease_owner is not None and run.lease_owner != lease_owner)
            ):
                raise RunEventRejectedError("研究运行不再接受新事件")
            seq = run.next_event_seq
            run.next_event_seq += 1
            session.add(
                RunEvent(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    seq=seq,
                    type=event_type,
                    event_key=event_key,
                    payload=payload,
                )
            )
            return seq

    def replay(self, run_id: UUID, *, after: int) -> list[PersistedRunEvent]:
        with self._session_factory() as session:
            events = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after)
                .order_by(RunEvent.seq)
            ).all()
        return [
            PersistedRunEvent(seq=event.seq, type=event.type, payload=event.payload)
            for event in events
        ]
