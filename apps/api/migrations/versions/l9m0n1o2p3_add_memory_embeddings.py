"""为长期 Memory 增加异步向量索引状态"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "l9m0n1o2p3"
down_revision: str | None = "k8l9m0n1o2"
branch_labels: str | None = None
depends_on: str | None = None


def _embedding_type() -> Any:
    """按数据库方言返回 pgvector 或 SQLite 兼容类型"""
    if op.get_bind().dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        return Vector()
    return sa.JSON()


def upgrade() -> None:
    """增加 Memory embedding 元数据和失败重试字段"""
    with op.batch_alter_table("memories") as batch_op:
        batch_op.add_column(sa.Column("embedding", _embedding_type(), nullable=True))
        batch_op.add_column(sa.Column("embedding_model", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("embedding_dimensions", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "embedding_status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(sa.Column("embedding_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_memories_workspace_embedding_status",
        "memories",
        ["workspace_id", "embedding_status"],
    )


def downgrade() -> None:
    """移除 Memory embedding 字段"""
    op.drop_index("ix_memories_workspace_embedding_status", table_name="memories")
    with op.batch_alter_table("memories") as batch_op:
        batch_op.drop_column("indexed_at")
        batch_op.drop_column("embedding_error")
        batch_op.drop_column("embedding_status")
        batch_op.drop_column("embedding_dimensions")
        batch_op.drop_column("embedding_model")
        batch_op.drop_column("embedding")
