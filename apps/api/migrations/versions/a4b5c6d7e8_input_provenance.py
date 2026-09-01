"""Persist canonical Sandbox Job input provenance."""

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8"
down_revision: str | None = "z3a4b5c6d7"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("sandbox_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("input_message_ids", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("input_attachment_ids", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("input_evidence_span_ids", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    raise RuntimeError("input provenance migration is intentionally irreversible")
