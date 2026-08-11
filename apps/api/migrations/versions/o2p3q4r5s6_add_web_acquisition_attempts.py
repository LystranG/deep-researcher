"""增加网页正文获取尝试审计表"""

import sqlalchemy as sa
from alembic import op

revision: str = "o2p3q4r5s6"
down_revision: str | None = "n1o2p3q4r5"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建按 Research Run 和来源顺序记录的获取尝试"""
    op.create_table(
        "web_acquisition_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("attempt_ordinal", sa.Integer(), nullable=False),
        sa.Column("adapter_id", sa.String(length=100), nullable=False),
        sa.Column("adapter_version", sa.String(length=100), nullable=False),
        sa.Column("requested_url", sa.String(length=4000), nullable=False),
        sa.Column("final_url", sa.String(length=4000), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("warning_category", sa.String(length=100), nullable=True),
        sa.Column("error_category", sa.String(length=100), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("completeness", sa.String(length=32), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"], ["source_snapshots.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "source_ordinal", "attempt_ordinal"),
    )
    op.create_index(
        op.f("ix_web_acquisition_attempts_run_id"),
        "web_acquisition_attempts",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_web_acquisition_attempts_source_snapshot_id"),
        "web_acquisition_attempts",
        ["source_snapshot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_web_acquisition_attempts_workspace_id"),
        "web_acquisition_attempts",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    """移除网页正文获取尝试审计表"""
    op.drop_index(
        op.f("ix_web_acquisition_attempts_workspace_id"),
        table_name="web_acquisition_attempts",
    )
    op.drop_index(
        op.f("ix_web_acquisition_attempts_source_snapshot_id"),
        table_name="web_acquisition_attempts",
    )
    op.drop_index(
        op.f("ix_web_acquisition_attempts_run_id"),
        table_name="web_acquisition_attempts",
    )
    op.drop_table("web_acquisition_attempts")
