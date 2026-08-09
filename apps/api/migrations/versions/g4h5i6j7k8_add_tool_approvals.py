"""增加工具调用、审批与执行记录

Revision ID: g4h5i6j7k8
Revises: f3g4h5i6j7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g4h5i6j7k8"
down_revision: str | Sequence[str] | None = "f3g4h5i6j7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加工具调用、一次性审批与幂等执行事实表"""
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("safe_summary", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "tool_name", "parameters_hash"),
    )
    op.create_index("ix_tool_calls_run_id", "tool_calls", ["run_id"])
    op.create_index("ix_tool_calls_workspace_id", "tool_calls", ["workspace_id"])
    op.create_table(
        "tool_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("requested_for_user_id", sa.Uuid(), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_for_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tool_call_id"], ["tool_calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_call_id"),
    )
    op.create_index("ix_tool_approvals_run_id", "tool_approvals", ["run_id"])
    op.create_index("ix_tool_approvals_tool_call_id", "tool_approvals", ["tool_call_id"], unique=True)
    op.create_index("ix_tool_approvals_workspace_id", "tool_approvals", ["workspace_id"])
    op.create_index(
        "ix_tool_approvals_requested_for_user_id",
        "tool_approvals",
        ["requested_for_user_id"],
    )
    op.create_table(
        "tool_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_summary", sa.String(length=2000), nullable=True),
        sa.Column("error_summary", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tool_call_id"], ["tool_calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_call_id"),
    )
    op.create_index("ix_tool_runs_run_id", "tool_runs", ["run_id"])
    op.create_index("ix_tool_runs_tool_call_id", "tool_runs", ["tool_call_id"], unique=True)
    op.create_index("ix_tool_runs_workspace_id", "tool_runs", ["workspace_id"])
    op.create_table(
        "workspace_mcp_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plugin_slug", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "plugin_slug"),
    )
    op.create_index(
        "ix_workspace_mcp_grants_workspace_id",
        "workspace_mcp_grants",
        ["workspace_id"],
    )


def downgrade() -> None:
    """移除工具调用、审批与执行事实表"""
    op.drop_index("ix_workspace_mcp_grants_workspace_id", table_name="workspace_mcp_grants")
    op.drop_table("workspace_mcp_grants")
    op.drop_index("ix_tool_runs_workspace_id", table_name="tool_runs")
    op.drop_index("ix_tool_runs_tool_call_id", table_name="tool_runs")
    op.drop_index("ix_tool_runs_run_id", table_name="tool_runs")
    op.drop_table("tool_runs")
    op.drop_index("ix_tool_approvals_requested_for_user_id", table_name="tool_approvals")
    op.drop_index("ix_tool_approvals_workspace_id", table_name="tool_approvals")
    op.drop_index("ix_tool_approvals_tool_call_id", table_name="tool_approvals")
    op.drop_index("ix_tool_approvals_run_id", table_name="tool_approvals")
    op.drop_table("tool_approvals")
    op.drop_index("ix_tool_calls_workspace_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_run_id", table_name="tool_calls")
    op.drop_table("tool_calls")
