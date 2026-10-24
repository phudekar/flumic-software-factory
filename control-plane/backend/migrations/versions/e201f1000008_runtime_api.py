"""ID-only dispatch outbox, pinned project follow-ups and builtin start authority."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000008"
down_revision = "e201f1000007"
branch_labels = depends_on = None


def project():
    return sa.Column(
        "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
    )


def upgrade():
    with op.batch_alter_table("execution_assignments") as batch:
        batch.add_column(sa.Column("started_at", sa.Float()))
    # A pre-upgrade running action is uncertain, never proven unstarted.
    op.execute(
        sa.text(
            "UPDATE execution_assignments SET started_at=created_at WHERE status='running'"
        )
    )
    op.create_table(
        "dynamic_dispatches",
        sa.Column("id", sa.String(36), primary_key=True),
        project(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("engine_id", sa.String(120)),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("error", sa.String(120)),
        sa.UniqueConstraint(
            "project_id", "kind", "subject_id", name="uq_dynamic_dispatch_subject"
        ),
    )
    op.create_index(
        "ix_dynamic_dispatches_project_id", "dynamic_dispatches", ["project_id"]
    )
    op.create_table(
        "project_followups",
        sa.Column("id", sa.String(36), primary_key=True),
        project(),
        sa.Column("source_execution_id", sa.String(36), nullable=False),
        sa.Column("source_run_id", sa.String(36), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("result_run_id", sa.String(36)),
        sa.Column("error", sa.String(120)),
        sa.UniqueConstraint(
            "project_id", "request_key", name="uq_project_followup_key"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_execution_id"],
            ["workflow_stage_attempts.project_id", "workflow_stage_attempts.id"],
            name="fk_followup_source",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_followup_source_run",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "result_run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_followup_result_run",
        ),
    )
    op.create_index(
        "ix_project_followups_project_id", "project_followups", ["project_id"]
    )


def downgrade():
    op.drop_table("project_followups")
    op.drop_table("dynamic_dispatches")
    with op.batch_alter_table("execution_assignments") as batch:
        batch.drop_column("started_at")
