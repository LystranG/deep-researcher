from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import ResearchRun


class RunQueue:
    """基于 PostgreSQL 行锁的 ResearchRun 队列"""

    def __init__(self, session_factory: sessionmaker[Session], *, lease_seconds: int = 30) -> None:
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds

    def claim(self, owner: str) -> UUID | None:
        """领取一个可执行运行并写入租约"""
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun)
                .where(
                    ResearchRun.cancel_requested_at.is_(None),
                    or_(
                        ResearchRun.status == "queued",
                        and_(
                            ResearchRun.status == "running",
                            ResearchRun.lease_expires_at.is_not(None),
                            ResearchRun.lease_expires_at < now,
                        ),
                    ),
                )
                .order_by(ResearchRun.created_at)
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return None
            run.status = "running"
            run.lease_owner = owner
            run.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            run.heartbeat_at = now
            run.attempt += 1
            return run.id

    def heartbeat(self, run_id: UUID, owner: str) -> bool:
        """延长指定 Worker 持有的运行租约"""
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(
                    ResearchRun.id == run_id,
                    ResearchRun.lease_owner == owner,
                    ResearchRun.status.in_({"queued", "running"}),
                )
            )
            if run is None:
                return False
            run.heartbeat_at = now
            run.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            return True

    def release(self, run_id: UUID, owner: str) -> None:
        """运行结束后清理 Worker 租约"""
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(
                    ResearchRun.id == run_id,
                    ResearchRun.lease_owner == owner,
                )
            )
            if run is None:
                return
            if run.status in {"completed", "partial", "cancelled", "failed"}:
                run.lease_owner = None
                run.lease_expires_at = None
