"""增加不可变 Plan v1、Task claim fencing 和唯一 Task Outcome"""

import sqlalchemy as sa
from alembic import op

revision: str = "r5s6t7u8v9"
down_revision: str | None = "q4r5s6t7u8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建 Runtime v2 第一阶段的业务事实"""
    op.create_table(
        "research_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "version"),
    )
    op.create_index("ix_research_plans_workspace_id", "research_plans", ["workspace_id"])
    op.create_index("ix_research_plans_run_id", "research_plans", ["run_id"])
    with op.batch_alter_table("research_tasks") as batch_op:
        batch_op.add_column(sa.Column("plan_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("goal", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("success_criteria", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("dependencies", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("local_budget", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"))
        batch_op.create_foreign_key(
            "fk_research_tasks_plan_id_research_plans",
            "research_plans",
            ["plan_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_research_tasks_plan_id", ["plan_id"])
    op.create_table(
        "task_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "fencing_epoch"),
    )
    for column in ("workspace_id", "run_id", "task_id"):
        op.create_index(f"ix_task_claims_{column}", "task_claims", [column])
    op.create_table(
        "task_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("outcome_ref", sa.String(length=200), nullable=False),
        sa.Column("result_reference", sa.String(length=1000), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("failure_ref", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
        sa.UniqueConstraint("outcome_ref"),
    )
    for column in ("workspace_id", "run_id", "task_id"):
        op.create_index(f"ix_task_outcomes_{column}", "task_outcomes", [column])


def downgrade() -> None:
    """移除 Runtime v2 第一阶段的业务事实"""
    for column in ("workspace_id", "run_id", "task_id"):
        op.drop_index(f"ix_task_outcomes_{column}", table_name="task_outcomes")
    op.drop_table("task_outcomes")
    for column in ("workspace_id", "run_id", "task_id"):
        op.drop_index(f"ix_task_claims_{column}", table_name="task_claims")
    op.drop_table("task_claims")
    with op.batch_alter_table("research_tasks") as batch_op:
        batch_op.drop_index("ix_research_tasks_plan_id")
        batch_op.drop_constraint("fk_research_tasks_plan_id_research_plans", type_="foreignkey")
        for column in (
            "fencing_epoch",
            "lease_expires_at",
            "lease_owner",
            "local_budget",
            "dependencies",
            "success_criteria",
            "goal",
            "plan_version",
            "plan_id",
        ):
            batch_op.drop_column(column)
    op.drop_index("ix_research_plans_run_id", table_name="research_plans")
    op.drop_index("ix_research_plans_workspace_id", table_name="research_plans")
    op.drop_table("research_plans")
