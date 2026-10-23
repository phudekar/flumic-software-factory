"""Encrypted immutable workflow revisions and durable launch pins."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000005"
down_revision = "e201f1000004"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("revision_id", sa.String(36)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_workflow_definition_name"),
        sa.UniqueConstraint("project_id", "id", name="uq_workflow_definition_identity"),
    )
    op.create_index(
        "ix_workflow_definitions_project_id", "workflow_definitions", ["project_id"]
    )
    op.create_table(
        "workflow_definition_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("definition_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("graph_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("project_id", "id", name="uq_workflow_revision_identity"),
        sa.UniqueConstraint(
            "project_id", "definition_id", "id", name="uq_workflow_revision_family"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "definition_id"],
            ["workflow_definitions.project_id", "workflow_definitions.id"],
            name="fk_workflow_revision_definition",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "definition_id", "parent_id"],
            [
                "workflow_definition_revisions.project_id",
                "workflow_definition_revisions.definition_id",
                "workflow_definition_revisions.id",
            ],
            name="fk_workflow_revision_parent",
        ),
    )
    op.create_index(
        "ix_workflow_definition_revisions_project_id",
        "workflow_definition_revisions",
        ["project_id"],
    )
    op.create_index(
        "ix_workflow_definition_revisions_definition_id",
        "workflow_definition_revisions",
        ["definition_id"],
    )
    with op.batch_alter_table("workflow_definitions") as batch:
        batch.create_foreign_key(
            "fk_workflow_definition_head",
            "workflow_definition_revisions",
            ["project_id", "id", "revision_id"],
            ["project_id", "definition_id", "id"],
        )
    op.create_table(
        "workflow_launch_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("workflow_revision_id", sa.String(36), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "workflow_revision_id"],
            [
                "workflow_definition_revisions.project_id",
                "workflow_definition_revisions.id",
            ],
            name="fk_workflow_launch_revision",
        ),
    )
    op.create_index(
        "ix_workflow_launch_snapshots_project_id",
        "workflow_launch_snapshots",
        ["project_id"],
    )


def downgrade():
    op.drop_table("workflow_launch_snapshots")
    # Remove only new definition data before dropping its cyclic head constraint.
    op.execute(sa.text("UPDATE workflow_definitions SET revision_id = NULL"))
    op.execute(sa.text("DELETE FROM workflow_definition_revisions"))
    with op.batch_alter_table("workflow_definitions") as batch:
        batch.drop_constraint("fk_workflow_definition_head", type_="foreignkey")
    op.drop_table("workflow_definition_revisions")
    op.drop_table("workflow_definitions")
