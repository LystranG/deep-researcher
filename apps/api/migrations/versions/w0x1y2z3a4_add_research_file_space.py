"""Add minimal durable Research File Space facts."""

import sqlalchemy as sa
from alembic import op

revision: str = "w0x1y2z3a4"
down_revision: str | None = "v9w0x1y2z3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_observations") as batch_op:
        batch_op.add_column(
            sa.Column("file_refs", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("evidence_spans") as batch_op:
        batch_op.add_column(sa.Column("file_ref", sa.JSON(), nullable=True))
    with op.batch_alter_table("citations") as batch_op:
        batch_op.add_column(sa.Column("file_ref", sa.JSON(), nullable=True))
    with op.batch_alter_table("derived_evidence") as batch_op:
        batch_op.add_column(
            sa.Column("input_file_refs", sa.JSON(), nullable=False, server_default="[]")
        )

    op.create_table(
        "research_source_mounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_name", sa.String(length=1000), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_entity_id", sa.Uuid(), nullable=False),
        sa.Column("source_revision", sa.String(length=100), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=2000), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "normalized_name"),
        sa.UniqueConstraint(
            "run_id", "source_type", "source_entity_id", "source_revision"
        ),
    )
    op.create_index(
        op.f("ix_research_source_mounts_workspace_id"),
        "research_source_mounts",
        ["workspace_id"],
    )
    op.create_index(
        op.f("ix_research_source_mounts_run_id"), "research_source_mounts", ["run_id"]
    )
    op.create_index(
        op.f("ix_research_source_mounts_source_entity_id"),
        "research_source_mounts",
        ["source_entity_id"],
    )

    op.create_table(
        "research_work_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_name", sa.String(length=1000), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["research_work_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "file_id", "revision"),
        sa.UniqueConstraint("task_id", "idempotency_key"),
    )
    for column in ("file_id", "workspace_id", "run_id", "task_id", "parent_revision_id"):
        op.create_index(
            op.f(f"ix_research_work_revisions_{column}"),
            "research_work_revisions",
            [column],
        )

    op.create_table(
        "research_artifact_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("published_by_task_id", sa.Uuid(), nullable=False),
        sa.Column("work_revision_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_name", sa.String(length=1000), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["published_by_task_id"], ["research_tasks.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["work_revision_id"], ["research_work_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "idempotency_key"),
        sa.UniqueConstraint("run_id", "normalized_name", "revision"),
    )
    for column in (
        "artifact_id",
        "workspace_id",
        "run_id",
        "published_by_task_id",
        "work_revision_id",
    ):
        op.create_index(
            op.f(f"ix_research_artifact_revisions_{column}"),
            "research_artifact_revisions",
            [column],
        )


def downgrade() -> None:
    op.drop_table("research_artifact_revisions")
    op.drop_table("research_work_revisions")
    op.drop_table("research_source_mounts")
    with op.batch_alter_table("derived_evidence") as batch_op:
        batch_op.drop_column("input_file_refs")
    with op.batch_alter_table("citations") as batch_op:
        batch_op.drop_column("file_ref")
    with op.batch_alter_table("evidence_spans") as batch_op:
        batch_op.drop_column("file_ref")
    with op.batch_alter_table("task_observations") as batch_op:
        batch_op.drop_column("file_refs")
