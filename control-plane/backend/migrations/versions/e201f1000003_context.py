"""Encrypted project/repository secrets and immutable action snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000003"
down_revision = "e201f1000002"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "secret_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("repository_id", sa.String(36)),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.Float()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("project_id", "id", name="uq_secret_project_identity"),
        sa.ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_secret_repository",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "parent_id"],
            ["secret_versions.project_id", "secret_versions.id"],
            name="fk_secret_parent",
        ),
    )
    op.create_index("ix_secret_versions_project_id", "secret_versions", ["project_id"])
    op.create_table(
        "action_context_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("execution_id", sa.String(120), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "project_id", "execution_id", name="uq_action_context_execution"
        ),
    )
    op.create_index(
        "ix_action_context_snapshots_project_id",
        "action_context_snapshots",
        ["project_id"],
    )


def downgrade():
    op.drop_table("action_context_snapshots")
    op.drop_table("secret_versions")
