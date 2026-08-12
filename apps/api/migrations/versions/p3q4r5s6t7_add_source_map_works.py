"""增加可恢复的 Source Chunk map work"""

import sqlalchemy as sa
from alembic import op

revision: str = "p3q4r5s6t7"
down_revision: str | None = "o2p3q4r5s6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建稳定身份与派生结果账本"""
    op.create_table(
        "source_map_works",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("chunk_ids", sa.JSON(), nullable=False),
        sa.Column("chunk_hashes", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("digest", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["ledger_id"], ["research_ledgers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"], ["source_snapshots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "input_hash"),
    )
    for column in ("ledger_id", "run_id", "source_snapshot_id", "workspace_id"):
        op.create_index(
            op.f(f"ix_source_map_works_{column}"),
            "source_map_works",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_source_map_works_run_status",
        "source_map_works",
        ["run_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """移除 Source Chunk map work 账本"""
    op.drop_index("ix_source_map_works_run_status", table_name="source_map_works")
    for column in ("workspace_id", "source_snapshot_id", "run_id", "ledger_id"):
        op.drop_index(op.f(f"ix_source_map_works_{column}"), table_name="source_map_works")
    op.drop_table("source_map_works")
