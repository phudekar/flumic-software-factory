"""Persist protected broker capability hashes and request reservations."""
from alembic import op
import sqlalchemy as sa

revision = "e201f1000016"
down_revision = "e201f1000015"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "execution_assignments",
        sa.Column("broker_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "broker_capabilities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("assignment_id", sa.String(36), nullable=False),
        sa.Column("audience", sa.String(104), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("bearer_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.UniqueConstraint("assignment_id", "audience", name="uq_broker_assignment_audience"),
        sa.ForeignKeyConstraint(
            ["project_id", "assignment_id"],
            ["execution_assignments.project_id", "execution_assignments.id"],
            name="fk_broker_assignment",
        ),
        sa.CheckConstraint(
            "audience IN ('model', 'repository') OR audience LIKE 'mcp:_%'",
            name="ck_broker_audience",
        ),
    )
    op.create_index("ix_broker_capabilities_project_id", "broker_capabilities", ["project_id"])
    op.create_table(
        "broker_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capability_id", sa.String(36), sa.ForeignKey("broker_capabilities.id"), nullable=False),
        sa.Column("request_id", sa.String(120), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("actual_usage", sa.JSON(), nullable=True),
        sa.UniqueConstraint("capability_id", "request_id", name="uq_broker_logical_request"),
        sa.CheckConstraint(
            "state IN ('reserved', 'dispatched', 'uncertain', 'completed', 'rejected')",
            name="ck_broker_reservation_state",
        ),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_broker_tokens"),
    )


def downgrade():
    db = op.get_bind()
    if (
        db.scalar(sa.text("SELECT 1 FROM broker_capabilities LIMIT 1"))
        or db.scalar(sa.text("SELECT 1 FROM execution_assignments WHERE broker_revoked LIMIT 1"))
    ):
        raise RuntimeError("Retain the protected broker ledger or perform a verified rollback first")
    op.drop_table("broker_reservations")
    op.drop_table("broker_capabilities")
    op.drop_column("execution_assignments", "broker_revoked")
