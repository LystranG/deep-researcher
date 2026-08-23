"""扩展 Tool Call 身份与可恢复 Observation 合同"""

import sqlalchemy as sa
from alembic import op

revision: str = "t7u8v9w0x1"
down_revision: str | None = "s6t7u8v9"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    """保存逻辑调用摘要、受限观察和耐久等待引用"""
    op.add_column(
        "task_model_turns", sa.Column("logical_call_ref", sa.String(length=300))
    )
    op.add_column(
        "task_model_turns", sa.Column("parameters_hash", sa.String(length=64))
    )
    op.add_column(
        "task_model_turns", sa.Column("safe_summary", sa.String(length=1000))
    )
    op.add_column(
        "task_observations", sa.Column("logical_call_ref", sa.String(length=300))
    )
    op.add_column("task_observations", sa.Column("summary", sa.String(length=2000)))
    op.add_column(
        "task_observations", sa.Column("error_category", sa.String(length=64))
    )
    op.add_column(
        "task_observations", sa.Column("waiting_reference", sa.String(length=500))
    )
    with op.batch_alter_table("task_observations") as batch:
        batch.drop_constraint("uq_task_observations_turn_id", type_="unique")

def downgrade() -> None:
    """移除 Tool Call 身份与可恢复 Observation 字段"""
    with op.batch_alter_table("task_observations") as batch:
        batch.create_unique_constraint("uq_task_observations_turn_id", ["turn_id"])
    op.drop_column("task_observations", "waiting_reference")
    op.drop_column("task_observations", "error_category")
    op.drop_column("task_observations", "summary")
    op.drop_column("task_observations", "logical_call_ref")
    op.drop_column("task_model_turns", "safe_summary")
    op.drop_column("task_model_turns", "parameters_hash")
    op.drop_column("task_model_turns", "logical_call_ref")
