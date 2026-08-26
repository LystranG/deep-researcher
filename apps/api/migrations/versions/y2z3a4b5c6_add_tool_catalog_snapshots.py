"""Persist the immutable tool catalog used by a Model Turn."""

import sqlalchemy as sa
from alembic import op

revision: str = "y2z3a4b5c6"
down_revision: str | None = "x1y2z3a4b5"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("task_model_turns", sa.Column("tool_snapshot", sa.JSON()))


def downgrade() -> None:
    op.drop_column("task_model_turns", "tool_snapshot")
