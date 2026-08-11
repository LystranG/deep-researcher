"""建立 Research Ledger 的覆盖、缺口和停止裁决事实"""

import sqlalchemy as sa
from alembic import op

revision: str = "m0n1o2p3q4"
down_revision: str | None = "l9m0n1o2p3"
branch_labels: str | None = None
depends_on: str | None = None


def _ledger_fk(table: str) -> sa.ForeignKeyConstraint:
    """返回账本表的统一 Workspace 外键"""
    return sa.ForeignKeyConstraint(["ledger_id"], ["research_ledgers.id"], ondelete="CASCADE")


def upgrade() -> None:
    """建立研究账本及其终态判断表"""
    op.create_table(
        "research_ledgers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_research_ledgers_workspace_id", "research_ledgers", ["workspace_id"])
    op.create_index("ix_research_ledgers_run_id", "research_ledgers", ["run_id"])

    op.create_table(
        "coverage_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("citation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _ledger_fk("coverage_snapshots"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coverage_snapshots_workspace_id", "coverage_snapshots", ["workspace_id"])
    op.create_index("ix_coverage_snapshots_ledger_id", "coverage_snapshots", ["ledger_id"])

    op.create_table(
        "evidence_gaps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _ledger_fk("evidence_gaps"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_gaps_workspace_id", "evidence_gaps", ["workspace_id"])
    op.create_index("ix_evidence_gaps_ledger_id", "evidence_gaps", ["ledger_id"])

    op.create_table(
        "stop_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("completeness", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _ledger_fk("stop_decisions"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_id"),
    )
    op.create_index("ix_stop_decisions_workspace_id", "stop_decisions", ["workspace_id"])
    op.create_index("ix_stop_decisions_ledger_id", "stop_decisions", ["ledger_id"])


def downgrade() -> None:
    """移除 Research Ledger 生命周期表"""
    op.drop_index("ix_stop_decisions_ledger_id", table_name="stop_decisions")
    op.drop_index("ix_stop_decisions_workspace_id", table_name="stop_decisions")
    op.drop_table("stop_decisions")
    op.drop_index("ix_evidence_gaps_ledger_id", table_name="evidence_gaps")
    op.drop_index("ix_evidence_gaps_workspace_id", table_name="evidence_gaps")
    op.drop_table("evidence_gaps")
    op.drop_index("ix_coverage_snapshots_ledger_id", table_name="coverage_snapshots")
    op.drop_index("ix_coverage_snapshots_workspace_id", table_name="coverage_snapshots")
    op.drop_table("coverage_snapshots")
    op.drop_index("ix_research_ledgers_run_id", table_name="research_ledgers")
    op.drop_index("ix_research_ledgers_workspace_id", table_name="research_ledgers")
    op.drop_table("research_ledgers")
