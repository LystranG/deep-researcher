"""Point sandbox facts at durable jobs and remove the legacy execution table."""

from alembic import op

revision: str = "z3a4b5c6d7"
down_revision: str | None = "y2z3a4b5c6"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("todos") as batch_op:
        batch_op.alter_column("sandbox_execution_id", new_column_name="sandbox_job_id")
        batch_op.create_foreign_key(
            "fk_todos_sandbox_job_id_sandbox_jobs",
            "sandbox_jobs",
            ["sandbox_job_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("derived_evidence") as batch_op:
        batch_op.alter_column(
            "sandbox_execution_id", new_column_name="sandbox_job_id"
        )
        batch_op.create_foreign_key(
            "fk_derived_evidence_sandbox_job_id_sandbox_jobs",
            "sandbox_jobs",
            ["sandbox_job_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.alter_column("sandbox_execution_id", new_column_name="sandbox_job_id")
        batch_op.create_foreign_key(
            "fk_artifacts_sandbox_job_id_sandbox_jobs",
            "sandbox_jobs",
            ["sandbox_job_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.drop_table("sandbox_executions")


def downgrade() -> None:
    raise RuntimeError("clean-cut migration is intentionally irreversible")
