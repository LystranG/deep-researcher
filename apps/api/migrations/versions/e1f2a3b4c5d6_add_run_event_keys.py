"""为 RunEvent 增加 Graph 投影幂等键

Revision ID: e1f2a3b4c5d6
Revises: d7e8f9a0b1c2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为事件投影增加可重放的幂等键"""
    op.add_column("run_events", sa.Column("event_key", sa.String(length=200), nullable=True))
    op.create_index(
        "ix_run_events_run_id_event_key",
        "run_events",
        ["run_id", "event_key"],
        unique=True,
    )


def downgrade() -> None:
    """移除事件投影幂等键"""
    op.drop_index("ix_run_events_run_id_event_key", table_name="run_events")
    op.drop_column("run_events", "event_key")
