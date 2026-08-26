"""Add durable Sandbox Jobs, Attempts and Observations."""

import sqlalchemy as sa
from alembic import op

revision: str = "x1y2z3a4b5"
down_revision: str | None = "w0x1y2z3a4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=True),
        sa.Column("invocation_key", sa.String(length=300), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("input_refs", sa.JSON(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('inspect_data', 'transform_artifact', 'derive_evidence')",
            name="ck_sandbox_jobs_purpose",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["research_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tool_call_id"], ["tool_calls.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invocation_key"),
    )
    for column in ("workspace_id", "run_id", "task_id", "status"):
        op.create_index(f"ix_sandbox_jobs_{column}", "sandbox_jobs", [column])
    op.create_table(
        "sandbox_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executor_node_id", sa.String(length=120), nullable=True),
        sa.Column("executor_reference", sa.String(length=300), nullable=True),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("output_staging_key", sa.String(length=500), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["sandbox_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number"),
    )
    op.create_index("ix_sandbox_attempts_job_id", "sandbox_attempts", ["job_id"])
    op.create_index("ix_sandbox_attempts_status", "sandbox_attempts", ["status"])
    op.create_table(
        "sandbox_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("stdout_preview", sa.Text(), nullable=False),
        sa.Column("stderr_preview", sa.Text(), nullable=False),
        sa.Column("file_refs", sa.JSON(), nullable=False),
        sa.Column("result_reference", sa.String(length=1000), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["sandbox_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["sandbox_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_sandbox_observations_job_id", "sandbox_observations", ["job_id"])


def downgrade() -> None:
    op.drop_table("sandbox_observations")
    op.drop_table("sandbox_attempts")
    op.drop_table("sandbox_jobs")
