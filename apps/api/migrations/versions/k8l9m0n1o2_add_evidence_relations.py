"""为研究记录增加 Claim、Evidence Span 和关系表"""

import sqlalchemy as sa
from alembic import op

revision: str = "k8l9m0n1o2"
down_revision: str | None = "j7k8l9m0n1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """建立不可变主张与独立证据片段关系"""
    op.create_table(
        "research_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False, server_default="verified"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="verified"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "content_hash"),
    )
    op.create_index(
        "ix_research_claims_workspace_id", "research_claims", ["workspace_id"]
    )
    op.create_index("ix_research_claims_run_id", "research_claims", ["run_id"])
    op.create_index(
        "ix_research_claims_workspace_status",
        "research_claims",
        ["workspace_id", "status"],
    )

    op.create_table(
        "evidence_spans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_chunk_id", sa.Uuid(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_chunk_id"], ["source_chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "source_chunk_id", "start_offset", "end_offset"),
    )
    op.create_index("ix_evidence_spans_workspace_id", "evidence_spans", ["workspace_id"])
    op.create_index("ix_evidence_spans_run_id", "evidence_spans", ["run_id"])
    op.create_index(
        "ix_evidence_spans_source_chunk_id", "evidence_spans", ["source_chunk_id"]
    )
    op.create_index(
        "ix_evidence_spans_workspace_run", "evidence_spans", ["workspace_id", "run_id"]
    )

    op.create_table(
        "research_claim_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_span_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False, server_default="supports"),
        sa.ForeignKeyConstraint(["claim_id"], ["research_claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["evidence_span_id"], ["evidence_spans.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_id", "evidence_span_id", "relation"),
    )
    op.create_index(
        "ix_research_claim_evidence_workspace_id",
        "research_claim_evidence",
        ["workspace_id"],
    )
    op.create_index(
        "ix_research_claim_evidence_claim_id", "research_claim_evidence", ["claim_id"]
    )
    op.create_index(
        "ix_research_claim_evidence_evidence_span_id",
        "research_claim_evidence",
        ["evidence_span_id"],
    )

    with op.batch_alter_table("research_records") as batch_op:
        batch_op.add_column(sa.Column("claim_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_research_records_claim_id",
            "research_claims",
            ["claim_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_research_records_claim_id", ["claim_id"])


def downgrade() -> None:
    """移除研究主张与证据关系表"""
    with op.batch_alter_table("research_records") as batch_op:
        batch_op.drop_index("ix_research_records_claim_id")
        batch_op.drop_constraint("fk_research_records_claim_id", type_="foreignkey")
        batch_op.drop_column("claim_id")

    op.drop_index(
        "ix_research_claim_evidence_evidence_span_id", table_name="research_claim_evidence"
    )
    op.drop_index("ix_research_claim_evidence_claim_id", table_name="research_claim_evidence")
    op.drop_index(
        "ix_research_claim_evidence_workspace_id", table_name="research_claim_evidence"
    )
    op.drop_table("research_claim_evidence")
    op.drop_index("ix_evidence_spans_workspace_run", table_name="evidence_spans")
    op.drop_index("ix_evidence_spans_source_chunk_id", table_name="evidence_spans")
    op.drop_index("ix_evidence_spans_run_id", table_name="evidence_spans")
    op.drop_index("ix_evidence_spans_workspace_id", table_name="evidence_spans")
    op.drop_table("evidence_spans")
    op.drop_index("ix_research_claims_workspace_status", table_name="research_claims")
    op.drop_index("ix_research_claims_run_id", table_name="research_claims")
    op.drop_index("ix_research_claims_workspace_id", table_name="research_claims")
    op.drop_table("research_claims")
