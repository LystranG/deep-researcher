"""增加 Sandbox 派生证据事实表"""

import sqlalchemy as sa
from alembic import op

revision: str = "q4r5s6t7u8"
down_revision: str | None = "p3q4r5s6t7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建可审计且不可变的派生证据记录"""
    op.add_column(
        "sandbox_executions",
        sa.Column(
            "input_message_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "sandbox_executions",
        sa.Column(
            "input_evidence_span_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.create_table(
        "derived_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_execution_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("input_message_ids", sa.JSON(), nullable=False),
        sa.Column("input_attachment_ids", sa.JSON(), nullable=False),
        sa.Column("input_evidence_span_ids", sa.JSON(), nullable=False),
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stdout_hash", sa.String(length=64), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["sandbox_execution_id"], ["sandbox_executions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sandbox_execution_id"),
    )
    for column in ("sandbox_execution_id", "run_id", "workspace_id"):
        op.create_index(
            op.f(f"ix_derived_evidence_{column}"),
            "derived_evidence",
            [column],
            unique=False,
        )
    with op.batch_alter_table("citations") as batch_op:
        batch_op.add_column(sa.Column("derived_evidence_id", sa.Uuid(), nullable=True))
        batch_op.alter_column(
            "source_chunk_id", existing_type=sa.Uuid(), nullable=True
        )
        batch_op.create_foreign_key(
            "fk_citations_derived_evidence_id_derived_evidence",
            "derived_evidence",
            ["derived_evidence_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_citations_exactly_one_source",
            "(source_chunk_id IS NOT NULL) != (derived_evidence_id IS NOT NULL)",
        )
    op.create_index(
        op.f("ix_citations_derived_evidence_id"),
        "citations",
        ["derived_evidence_id"],
        unique=False,
    )


def downgrade() -> None:
    """移除派生证据事实表"""
    op.drop_index(op.f("ix_citations_derived_evidence_id"), table_name="citations")
    with op.batch_alter_table("citations") as batch_op:
        batch_op.drop_constraint("ck_citations_exactly_one_source", type_="check")
        batch_op.drop_constraint(
            "fk_citations_derived_evidence_id_derived_evidence", type_="foreignkey"
        )
        batch_op.drop_column("derived_evidence_id")
        batch_op.alter_column(
            "source_chunk_id", existing_type=sa.Uuid(), nullable=False
        )
    for column in ("workspace_id", "run_id", "sandbox_execution_id"):
        op.drop_index(op.f(f"ix_derived_evidence_{column}"), table_name="derived_evidence")
    op.drop_table("derived_evidence")
    op.drop_column("sandbox_executions", "input_evidence_span_ids")
    op.drop_column("sandbox_executions", "input_message_ids")
