"""为网页来源快照增加顺序和内容类型"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i6j7k8l9m0"
down_revision: str | None = "h5i6j7k8l9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加来源顺序和网页内容状态"""
    with op.batch_alter_table("source_snapshots") as batch_op:
        batch_op.add_column(
            sa.Column("content_kind", sa.String(length=32), nullable=False, server_default="search_snippet")
        )
        batch_op.add_column(sa.Column("ordinal", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    """移除来源顺序和内容状态字段"""
    with op.batch_alter_table("source_snapshots") as batch_op:
        batch_op.drop_column("ordinal")
        batch_op.drop_column("content_kind")
