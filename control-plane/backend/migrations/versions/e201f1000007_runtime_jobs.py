"""Durable one-time Docker job intents and terminal captures."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000007"
down_revision = "e201f1000006"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "runtime_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("assignment_id", sa.String(36), nullable=False),
        sa.Column("spec_digest", sa.String(64), nullable=False),
        sa.Column("launch_ciphertext", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("launch_started", sa.Boolean(), nullable=False),
        sa.Column("runtime_observed", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("deadline", sa.Float(), nullable=False),
        sa.Column("result_ciphertext", sa.Text()),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("next_cleanup_at", sa.Float(), nullable=False),
        sa.Column("error", sa.String(120)),
        sa.UniqueConstraint(
            "project_id", "execution_id", name="uq_runtime_job_execution"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "execution_id"],
            ["workflow_stage_attempts.project_id", "workflow_stage_attempts.id"],
            name="fk_runtime_job_execution",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "assignment_id"],
            ["execution_assignments.project_id", "execution_assignments.id"],
            name="fk_runtime_job_assignment",
        ),
    )
    op.create_index("ix_runtime_jobs_project_id", "runtime_jobs", ["project_id"])


def downgrade():
    op.drop_table("runtime_jobs")
