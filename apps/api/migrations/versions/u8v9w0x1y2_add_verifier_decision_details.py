"""保存 Verifier 的逐项覆盖和四路裁决详情"""

import sqlalchemy as sa
from alembic import op

revision: str = "u8v9w0x1y2"
down_revision: str | None = "t7u8v9w0x1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """扩展账本终态事实，保留旧 API 可读字段。"""
    op.add_column(
        "coverage_snapshots",
        sa.Column("items", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "stop_decisions",
        sa.Column("route", sa.String(length=32), nullable=False, server_default="failed"),
    )
    op.add_column(
        "stop_decisions",
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    """移除 Verifier 扩展字段。"""
    op.drop_column("stop_decisions", "details")
    op.drop_column("stop_decisions", "route")
    op.drop_column("coverage_snapshots", "items")
