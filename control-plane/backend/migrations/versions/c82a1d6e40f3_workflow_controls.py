"""Durable workflow control receipts; request bodies are never persisted."""

from alembic import op
import sqlalchemy as sa

revision = "c82a1d6e40f3"
down_revision = "b47c02a15f01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_control_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(160), nullable=False, unique=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(36), nullable=False),
        sa.Column("workflow_name", sa.String(120), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(240)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_workflow_control_operations_workflow_id",
        "workflow_control_operations",
        ["workflow_id"],
    )


def downgrade():
    op.drop_table("workflow_control_operations")
