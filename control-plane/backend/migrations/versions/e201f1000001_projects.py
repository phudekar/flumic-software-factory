"""Add project ownership without rewriting legacy configuration or worker state."""
from alembic import op
import sqlalchemy as sa

revision = "e201f1000001"
down_revision = "c82a1d6e40f3"
branch_labels = None
depends_on = None
DEFAULT = "00000000-0000-0000-0000-000000000001"
OWNED = ("config_revisions", "worker_configurations", "execution_events", "conversation_content", "workflow_control_operations")


def upgrade():
    projects = op.create_table("projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("defaults", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False))
    op.bulk_insert(projects, [{"id":DEFAULT,"slug":"default","name":"Default project","defaults":{},"created_at":0.0}])
    op.create_table("repository_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("alias", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("host", sa.String(240), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("credential_id", sa.String(36)),
        sa.Column("revision_id", sa.String(36)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("project_id", "alias", name="uq_repository_project_alias"),
        sa.UniqueConstraint("project_id", "id", name="uq_repository_project_identity"))
    op.create_table("repository_revisions",
        sa.Column("id",sa.String(36),primary_key=True),
        sa.Column("project_id",sa.String(36),sa.ForeignKey("projects.id"),nullable=False,server_default=DEFAULT),
        sa.Column("repository_id",sa.String(36),nullable=False),
        sa.Column("parent_id",sa.String(36)),
        sa.Column("document",sa.JSON(),nullable=False),
        sa.Column("created_at",sa.Float(),nullable=False),
        sa.ForeignKeyConstraint(["project_id","repository_id"],["repository_connections.project_id","repository_connections.id"],name="fk_repository_revision_owner"))
    op.create_index("ix_repository_revisions_project_id","repository_revisions",["project_id"])
    op.create_index("ix_repository_revisions_repository_id","repository_revisions",["repository_id"])
    op.create_index("ix_repository_connections_project_id", "repository_connections", ["project_id"])
    for table in OWNED:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("project_id", sa.String(36), nullable=False, server_default=DEFAULT))
            batch.create_foreign_key("fk_" + table + "_project", "projects", ["project_id"], ["id"])
            batch.create_index("ix_" + table + "_project_id", ["project_id"])
    with op.batch_alter_table("execution_events") as batch:
        batch.add_column(sa.Column("repository_id", sa.String(36)))
        batch.create_foreign_key("fk_execution_events_repository", "repository_connections", ["repository_id"], ["id"])


def downgrade():
    with op.batch_alter_table("execution_events") as batch:
        batch.drop_constraint("fk_execution_events_repository", type_="foreignkey")
        batch.drop_column("repository_id")
    for table in reversed(OWNED):
        with op.batch_alter_table(table) as batch:
            batch.drop_index("ix_" + table + "_project_id")
            batch.drop_constraint("fk_" + table + "_project", type_="foreignkey")
            batch.drop_column("project_id")
    op.drop_table("repository_revisions")
    op.drop_table("repository_connections")
    op.drop_table("projects")
