"""Durable runs, logical attempts, scoped state and external-resource fences."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000006"
down_revision = "e201f1000005"
branch_labels = depends_on = None


def project():
    return sa.Column(
        "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
    )


def upgrade():
    with op.batch_alter_table("execution_assignments") as batch:
        batch.create_unique_constraint("uq_assignment_owner", ["project_id", "id"])
    with op.batch_alter_table("workflow_launch_snapshots") as batch:
        batch.create_unique_constraint("uq_workflow_launch_owner", ["project_id", "id"])
    op.create_table(
        "workflow_admission",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("last_project", sa.String(36), nullable=False),
        sa.Column("tick", sa.Integer(), nullable=False),
    )
    op.execute(
        sa.text(
            "INSERT INTO workflow_admission(id,last_project,tick) VALUES ('global','',0)"
        )
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        project(),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(120), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("input_ciphertext", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("project_id", "id", name="uq_workflow_run_owner"),
        sa.UniqueConstraint(
            "project_id", "request_key", name="uq_workflow_run_request"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "snapshot_id"],
            ["workflow_launch_snapshots.project_id", "workflow_launch_snapshots.id"],
            name="fk_run_snapshot",
        ),
    )
    op.create_index("ix_workflow_runs_project_id", "workflow_runs", ["project_id"])
    op.create_table(
        "workflow_stage_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        project(),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("stage_id", sa.String(100), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("assignment_id", sa.String(36)),
        sa.Column("spec_digest", sa.String(64)),
        sa.Column("input_digest", sa.String(64)),
        sa.Column("origins_ciphertext", sa.Text()),
        sa.Column("result_digest", sa.String(64)),
        sa.Column("result_ciphertext", sa.Text()),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["workflow_runs.project_id", "workflow_runs.id"],
            name="fk_attempt_run",
        ),
        sa.UniqueConstraint(
            "project_id", "run_id", "stage_id", "generation", name="uq_stage_generation"
        ),
        sa.UniqueConstraint("project_id", "id", name="uq_attempt_owner"),
        sa.ForeignKeyConstraint(
            ["project_id", "assignment_id"],
            ["execution_assignments.project_id", "execution_assignments.id"],
            name="fk_attempt_assignment",
        ),
    )
    op.create_index(
        "ix_workflow_stage_attempts_project_id",
        "workflow_stage_attempts",
        ["project_id"],
    )
    op.create_index(
        "ix_workflow_stage_attempts_run_id", "workflow_stage_attempts", ["run_id"]
    )
    op.create_table(
        "project_business_state",
        project(),
        sa.Column("repository_id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_business_state_repository",
        ),
    )
    op.create_index(
        "ix_project_business_state_project_id", "project_business_state", ["project_id"]
    )
    op.create_table(
        "external_resource_claims",
        sa.Column("resource", sa.String(512), primary_key=True),
        project(),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "execution_id"],
            ["workflow_stage_attempts.project_id", "workflow_stage_attempts.id"],
            name="fk_resource_execution",
        ),
    )
    op.create_table(
        "repository_execution_modes",
        sa.Column("repository", sa.String(300), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
    )


def downgrade():
    op.drop_table("repository_execution_modes")
    op.drop_table("external_resource_claims")
    op.drop_table("project_business_state")
    op.drop_table("workflow_stage_attempts")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_admission")
    with op.batch_alter_table("workflow_launch_snapshots") as batch:
        batch.drop_constraint("uq_workflow_launch_owner", type_="unique")
    with op.batch_alter_table("execution_assignments") as batch:
        batch.drop_constraint("uq_assignment_owner", type_="unique")
