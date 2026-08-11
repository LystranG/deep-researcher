"""为会话分段增加跨会话可见范围"""

import sqlalchemy as sa
from alembic import op

revision: str = "n1o2p3q4r5"
down_revision: str | None = "m0n1o2p3q4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """增加会话分段可见范围并保守隔离无法重算来源的存量数据"""
    with op.batch_alter_table("conversation_segments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "visibility_scope",
                sa.String(length=32),
                nullable=True,
            )
        )
    op.execute("UPDATE conversation_segments SET visibility_scope = 'conversation'")
    with op.batch_alter_table("conversation_segments") as batch_op:
        batch_op.alter_column(
            "visibility_scope",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="workspace",
        )
    op.execute(
        """
        UPDATE research_records
        SET deleted_at = CURRENT_TIMESTAMP
        WHERE deleted_at IS NULL
          AND (
            claim_id IS NULL
            OR EXISTS (
                SELECT 1
                FROM research_claim_evidence AS relation
                JOIN evidence_spans AS span ON span.id = relation.evidence_span_id
                JOIN source_chunks AS chunk ON chunk.id = span.source_chunk_id
                JOIN attachments AS attachment ON attachment.id = chunk.attachment_id
                WHERE relation.claim_id = research_records.claim_id
                  AND attachment.promoted_document_id IS NULL
            )
          )
        """
    )


def downgrade() -> None:
    """移除会话分段可见范围"""
    with op.batch_alter_table("conversation_segments") as batch_op:
        batch_op.drop_column("visibility_scope")
