"""增加研究运行预算预留与用量台账

Revision ID: f3g4h5i6j7
Revises: e1f2a3b4c5d6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3g4h5i6j7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加运行预算与 Workspace 配额台账"""
    op.add_column(
        "research_runs",
        sa.Column("reservation_status", sa.String(length=20), nullable=False, server_default="none"),
    )
    op.add_column(
        "research_runs",
        sa.Column("reserved_token_budget", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "research_runs",
        sa.Column("reserved_cost_micros", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "quota_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("token_limit", sa.Integer(), nullable=False),
        sa.Column("cost_limit_micros", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_micros", sa.Integer(), nullable=False),
        sa.Column("used_tokens", sa.Integer(), nullable=False),
        sa.Column("used_cost_micros", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id"),
    )
    op.create_index("ix_quota_policies_workspace_id", "quota_policies", ["workspace_id"], unique=True)
    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("call_kind", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "call_kind"),
    )
    op.create_index("ix_usage_ledger_workspace_id", "usage_ledger", ["workspace_id"])
    op.create_index("ix_usage_ledger_run_id", "usage_ledger", ["run_id"])
    op.create_index("ix_usage_ledger_task_id", "usage_ledger", ["task_id"])


def downgrade() -> None:
    """移除运行预算与 Workspace 配额台账"""
    op.drop_index("ix_usage_ledger_task_id", table_name="usage_ledger")
    op.drop_index("ix_usage_ledger_run_id", table_name="usage_ledger")
    op.drop_index("ix_usage_ledger_workspace_id", table_name="usage_ledger")
    op.drop_table("usage_ledger")
    op.drop_index("ix_quota_policies_workspace_id", table_name="quota_policies")
    op.drop_table("quota_policies")
    op.drop_column("research_runs", "reserved_cost_micros")
    op.drop_column("research_runs", "reserved_token_budget")
    op.drop_column("research_runs", "reservation_status")
