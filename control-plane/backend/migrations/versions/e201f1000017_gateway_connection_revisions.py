"""Persist immutable project-owned gateway connection revisions."""
from alembic import op
import sqlalchemy as sa

revision = "e201f1000017"
down_revision = "e201f1000016"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "gateway_connection_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False,
                  server_default="00000000-0000-0000-0000-000000000001"),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("endpoint", sa.String(500), nullable=False),
        sa.Column("inference_credential_id", sa.String(36), nullable=False),
        sa.Column("management_credential_id", sa.String(36)),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("project_id", "id", name="uq_gateway_connection_revision_owner"),
        sa.ForeignKeyConstraint(
            ["project_id", "parent_id"],
            ["gateway_connection_revisions.project_id", "gateway_connection_revisions.id"],
            name="fk_gateway_connection_parent",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "inference_credential_id"],
            ["secret_versions.project_id", "secret_versions.id"],
            name="fk_gateway_inference_credential",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "management_credential_id"],
            ["secret_versions.project_id", "secret_versions.id"],
            name="fk_gateway_management_credential",
        ),
    )
    op.create_index("ix_gateway_connection_revisions_project_id", "gateway_connection_revisions", ["project_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT 1 FROM gateway_connection_revisions LIMIT 1")):
        raise RuntimeError("Retain gateway connection revisions or perform a verified rollback first")
    op.drop_table("gateway_connection_revisions")
