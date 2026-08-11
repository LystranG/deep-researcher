"""为混合检索增加 embedding、会话分段和研究记录"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "j7k8l9m0n1"
down_revision: str | None = "i6j7k8l9m0"
branch_labels: str | None = None
depends_on: str | None = None


def _embedding_type() -> Any:
    """按数据库方言返回 pgvector 或 SQLite 兼容类型"""
    if op.get_bind().dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        return Vector()
    return sa.JSON()


def _add_embedding_columns(batch_op: Any, embedding_type: Any) -> None:
    """为既有 SourceChunk 增加可重试的 embedding 索引状态"""
    batch_op.add_column(sa.Column("embedding", embedding_type, nullable=True))
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


def upgrade() -> None:
    """建立向量召回所需的持久化字段和跨会话记录"""
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"
    if is_postgresql:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    embedding_type = _embedding_type()
    with op.batch_alter_table("source_chunks") as batch_op:
        _add_embedding_columns(batch_op, embedding_type)

    op.create_index(
        "ix_source_chunks_workspace_embedding_status",
        "source_chunks",
        ["workspace_id", "embedding_status"],
    )

    op.create_table(
        "conversation_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("first_message_id", sa.Uuid(), nullable=True),
        sa.Column("last_message_id", sa.Uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", embedding_type, nullable=True),
        sa.Column("embedding_model", sa.String(length=200), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column(
            "embedding_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("embedding_error", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["first_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["last_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "ordinal"),
    )
    op.create_index(
        "ix_conversation_segments_workspace_id", "conversation_segments", ["workspace_id"]
    )
    op.create_index(
        "ix_conversation_segments_conversation_id", "conversation_segments", ["conversation_id"]
    )
    op.create_index(
        "ix_conversation_segments_first_message_id", "conversation_segments", ["first_message_id"]
    )
    op.create_index(
        "ix_conversation_segments_last_message_id", "conversation_segments", ["last_message_id"]
    )
    op.create_index(
        "ix_conversation_segments_workspace_embedding_status",
        "conversation_segments",
        ["workspace_id", "embedding_status"],
    )

    op.create_table(
        "research_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("record_key", sa.String(length=200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("claim_kind", sa.String(length=32), nullable=False, server_default="fact"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("embedding", embedding_type, nullable=True),
        sa.Column("embedding_model", sa.String(length=200), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column(
            "embedding_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("embedding_error", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["research_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "record_key", "version"),
    )
    op.create_index("ix_research_records_workspace_id", "research_records", ["workspace_id"])
    op.create_index("ix_research_records_run_id", "research_records", ["run_id"])
    op.create_index("ix_research_records_supersedes_id", "research_records", ["supersedes_id"])
    op.create_index(
        "ix_research_records_workspace_status", "research_records", ["workspace_id", "status"]
    )
    op.create_index(
        "ix_research_records_workspace_embedding_status",
        "research_records",
        ["workspace_id", "embedding_status"],
    )

    if is_postgresql:
        op.create_index(
            "ix_source_chunks_text_fts",
            "source_chunks",
            [sa.text("to_tsvector('simple', text)")],
            postgresql_using="gin",
        )
        op.create_index(
            "ix_conversation_segments_text_fts",
            "conversation_segments",
            [sa.text("to_tsvector('simple', text)")],
            postgresql_using="gin",
        )
        op.create_index(
            "ix_research_records_claim_text_fts",
            "research_records",
            [sa.text("to_tsvector('simple', claim_text)")],
            postgresql_using="gin",
        )


def downgrade() -> None:
    """移除混合检索字段、会话分段和研究记录"""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index("ix_research_records_claim_text_fts", table_name="research_records")
        op.drop_index("ix_conversation_segments_text_fts", table_name="conversation_segments")
        op.drop_index("ix_source_chunks_text_fts", table_name="source_chunks")

    op.drop_index(
        "ix_research_records_workspace_embedding_status", table_name="research_records"
    )
    op.drop_index("ix_research_records_workspace_status", table_name="research_records")
    op.drop_index("ix_research_records_supersedes_id", table_name="research_records")
    op.drop_index("ix_research_records_run_id", table_name="research_records")
    op.drop_index("ix_research_records_workspace_id", table_name="research_records")
    op.drop_table("research_records")

    op.drop_index(
        "ix_conversation_segments_workspace_embedding_status",
        table_name="conversation_segments",
    )
    op.drop_index("ix_conversation_segments_last_message_id", table_name="conversation_segments")
    op.drop_index("ix_conversation_segments_first_message_id", table_name="conversation_segments")
    op.drop_index("ix_conversation_segments_conversation_id", table_name="conversation_segments")
    op.drop_index("ix_conversation_segments_workspace_id", table_name="conversation_segments")
    op.drop_table("conversation_segments")

    op.drop_index("ix_source_chunks_workspace_embedding_status", table_name="source_chunks")
    with op.batch_alter_table("source_chunks") as batch_op:
        batch_op.drop_column("indexed_at")
        batch_op.drop_column("embedding_error")
        batch_op.drop_column("embedding_status")
        batch_op.drop_column("embedding_dimensions")
        batch_op.drop_column("embedding_model")
        batch_op.drop_column("embedding")
