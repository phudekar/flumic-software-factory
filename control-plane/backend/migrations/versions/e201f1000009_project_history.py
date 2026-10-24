"""Add nullable action attribution without reinterpreting historical events."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000009"
down_revision = "e201f1000008"
branch_labels = depends_on = None

COLUMNS = {
    "assignment_id": 36,
    "execution_id": 120,
    "definition_id": 36,
    "workflow_revision_id": 36,
    "executor": 100,
    "job_id": 64,
    "call_id": 180,
    "ingest_digest": 64,
}
INDEXED = (
    "assignment_id",
    "execution_id",
    "definition_id",
    "workflow_revision_id",
    "executor",
    "job_id",
)


def upgrade():
    with op.batch_alter_table("worker_instances") as batch:
        batch.add_column(sa.Column("last_heartbeat_at", sa.Float()))
    with op.batch_alter_table("execution_events") as batch:
        for name, length in COLUMNS.items():
            batch.add_column(sa.Column(name, sa.String(length)))
        batch.create_unique_constraint(
            "uq_action_model_call", ["project_id", "execution_id", "call_id"]
        )
        for name in INDEXED:
            batch.create_index("ix_execution_events_" + name, [name])


def downgrade():
    with op.batch_alter_table("execution_events") as batch:
        for name in INDEXED:
            batch.drop_index("ix_execution_events_" + name)
        batch.drop_constraint("uq_action_model_call", type_="unique")
        for name in reversed(COLUMNS):
            batch.drop_column(name)
    with op.batch_alter_table("worker_instances") as batch:
        batch.drop_column("last_heartbeat_at")
