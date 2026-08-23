"""增加有界 Model Turn、Tool Observation 和结果提案事实"""

import sqlalchemy as sa
from alembic import op

revision: str = "s6t7u8v9"
down_revision: str | None = "r5s6t7u8v9"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建单 Task ReAct 推进所需的 append-only 事实"""
    op.create_table(
        "task_model_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("turn_ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("output_kind", sa.String(length=32), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("provider_reference", sa.String(length=500), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "turn_ordinal"),
    )
    for column in ("workspace_id", "run_id", "task_id"):
        op.create_index(f"ix_task_model_turns_{column}", "task_model_turns", [column])

    op.create_table(
        "task_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("observation_ref", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_reference", sa.String(length=1000), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evidence_gain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failure_ref", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["task_model_turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "observation_ref"),
        sa.UniqueConstraint("turn_id", name="uq_task_observations_turn_id"),
    )
    for column in ("workspace_id", "run_id", "task_id", "turn_id"):
        op.create_index(f"ix_task_observations_{column}", "task_observations", [column])

    op.create_table(
        "task_result_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("result_reference", sa.String(length=1000), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("covered_criteria", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rejection_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["task_model_turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id"),
    )
    for column in ("workspace_id", "run_id", "task_id", "turn_id"):
        op.create_index(f"ix_task_result_proposals_{column}", "task_result_proposals", [column])


def downgrade() -> None:
    """移除有界 Model Turn 事实"""
    for column in ("workspace_id", "run_id", "task_id", "turn_id"):
        op.drop_index(f"ix_task_result_proposals_{column}", table_name="task_result_proposals")
    op.drop_table("task_result_proposals")
    for column in ("workspace_id", "run_id", "task_id", "turn_id"):
        op.drop_index(f"ix_task_observations_{column}", table_name="task_observations")
    op.drop_table("task_observations")
    for column in ("workspace_id", "run_id", "task_id"):
        op.drop_index(f"ix_task_model_turns_{column}", table_name="task_model_turns")
    op.drop_table("task_model_turns")
