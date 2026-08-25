"""Persist controlled plan revision provenance."""

import sqlalchemy as sa
from alembic import op

revision: str = "v9w0x1y2z3"
down_revision: str | None = "u8v9w0x1y2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("research_plans") as batch_op:
        batch_op.add_column(sa.Column("trigger_type", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("trigger_ref", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("decision_ref", sa.String(length=500), nullable=True))
        batch_op.add_column(
            sa.Column("inherited_task_ordinals", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("replaced_task_ordinals", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("research_plans") as batch_op:
        batch_op.drop_column("replaced_task_ordinals")
        batch_op.drop_column("inherited_task_ordinals")
        batch_op.drop_column("decision_ref")
        batch_op.drop_column("trigger_ref")
        batch_op.drop_column("trigger_type")
