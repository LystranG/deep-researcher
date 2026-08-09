"""增加 Agent Todo 持久化事实表

Revision ID: h5i6j7k8l9
Revises: g4h5i6j7k8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h5i6j7k8l9"
down_revision: str | Sequence[str] | None = "g4h5i6j7k8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建可恢复、可审计的研究 Todo 事实表"""
    op.create_table(
        "todos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("research_task_id", sa.Uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("sandbox_execution_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["research_task_id"], ["research_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sandbox_execution_id"], ["sandbox_executions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "idempotency_key"),
    )
    op.create_index("ix_todos_workspace_id", "todos", ["workspace_id"])
    op.create_index("ix_todos_run_id", "todos", ["run_id"])
    op.create_index("ix_todos_research_task_id", "todos", ["research_task_id"])
    op.create_index("ix_todos_sandbox_execution_id", "todos", ["sandbox_execution_id"])


def downgrade() -> None:
    """移除 Agent Todo 事实表"""
    op.drop_index("ix_todos_sandbox_execution_id", table_name="todos")
    op.drop_index("ix_todos_research_task_id", table_name="todos")
    op.drop_index("ix_todos_run_id", table_name="todos")
    op.drop_index("ix_todos_workspace_id", table_name="todos")
    op.drop_table("todos")
