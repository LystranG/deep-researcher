"""Point sandbox facts at durable jobs and remove the legacy execution table."""

from alembic import op

revision: str = "z3a4b5c6d7"
down_revision: str | None = "y2z3a4b5c6"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    batch_kwargs = {
        "naming_convention": {
            "fk": "%(table_name)s_%(column_0_name)s_fkey",
        }
    }

    with op.batch_alter_table("todos", **batch_kwargs) as batch_op:
        batch_op.drop_constraint("todos_sandbox_execution_id_fkey", type_="foreignkey")
        batch_op.alter_column("sandbox_execution_id", new_column_name="sandbox_job_id")
        batch_op.create_foreign_key(
            "fk_todos_sandbox_job_id_sandbox_jobs",
            "sandbox_jobs",
            ["sandbox_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.drop_index("ix_todos_sandbox_execution_id", table_name="todos")
    op.create_index("ix_todos_sandbox_job_id", "todos", ["sandbox_job_id"])

    with op.batch_alter_table("derived_evidence", **batch_kwargs) as batch_op:
        batch_op.drop_constraint(
            "derived_evidence_sandbox_execution_id_fkey", type_="foreignkey"
        )
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
    op.drop_index("ix_derived_evidence_sandbox_execution_id", table_name="derived_evidence")
    op.create_index("ix_derived_evidence_sandbox_job_id", "derived_evidence", ["sandbox_job_id"])

    with op.batch_alter_table("artifacts", **batch_kwargs) as batch_op:
        batch_op.drop_constraint(
            "artifacts_sandbox_execution_id_fkey", type_="foreignkey"
        )
        batch_op.alter_column("sandbox_execution_id", new_column_name="sandbox_job_id")
        batch_op.create_foreign_key(
            "fk_artifacts_sandbox_job_id_sandbox_jobs",
            "sandbox_jobs",
            ["sandbox_job_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.drop_index("ix_artifacts_sandbox_execution_id", table_name="artifacts")
    op.create_index("ix_artifacts_sandbox_job_id", "artifacts", ["sandbox_job_id"])

    op.drop_table("sandbox_executions")


def downgrade() -> None:
    raise RuntimeError("clean-cut migration is intentionally irreversible")
