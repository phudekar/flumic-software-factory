"""Independent immutable worker configurations; preserve existing snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "b47c02a15f01"
down_revision = "914e381e2a9c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "config_revisions",
        sa.Column("scope", sa.String(32), nullable=False, server_default="factory"),
    )
    op.add_column(
        "worker_instances", sa.Column("config_id", sa.String(36), nullable=True)
    )
    op.create_table(
        "worker_configurations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("cloned_from", sa.String(36)),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("profile", sa.String(100), nullable=False),
        sa.Column("factory_revision_id", sa.String(36), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_worker_configurations_family_id", "worker_configurations", ["family_id"]
    )


def downgrade():
    op.drop_table("worker_configurations")
    op.drop_column("worker_instances", "config_id")
    op.drop_column("config_revisions", "scope")
