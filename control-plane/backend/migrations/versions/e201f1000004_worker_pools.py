"""Shared pool protocol and scoped, encrypted execution assignments."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000004"
down_revision = "e201f1000003"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "worker_pools",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "project_worker_pools",
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), primary_key=True
        ),
        sa.Column(
            "pool_id", sa.String(36), sa.ForeignKey("worker_pools.id"), primary_key=True
        ),
    )
    with op.batch_alter_table("worker_instances") as batch:
        batch.add_column(
            sa.Column(
                "protocol", sa.String(20), nullable=False, server_default="legacy"
            )
        )
        batch.add_column(sa.Column("pool_id", sa.String(36)))
        batch.add_column(
            sa.Column("capabilities", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(sa.Column("registered_at", sa.Float()))
        batch.create_foreign_key("fk_worker_pool", "worker_pools", ["pool_id"], ["id"])
    op.create_table(
        "execution_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("repository_id", sa.String(36), nullable=False),
        sa.Column(
            "pool_id", sa.String(36), sa.ForeignKey("worker_pools.id"), nullable=False
        ),
        sa.Column("run_id", sa.String(120), nullable=False),
        sa.Column("stage_id", sa.String(100), nullable=False),
        sa.Column("execution_id", sa.String(120), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("executor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("worker_id", sa.String(36), sa.ForeignKey("worker_instances.id")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("claim_hash", sa.String(64)),
        sa.Column("expires_at", sa.Float()),
        sa.Column(
            "context_id", sa.String(36), sa.ForeignKey("action_context_snapshots.id")
        ),
        sa.Column("input_ciphertext", sa.Text(), nullable=False),
        sa.Column("result_ciphertext", sa.Text()),
        sa.Column("result_digest", sa.String(64)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "project_id", "execution_id", name="uq_assignment_execution"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "repository_id"],
            ["repository_connections.project_id", "repository_connections.id"],
            name="fk_assignment_repository",
        ),
    )
    op.create_index(
        "ix_execution_assignments_project_id", "execution_assignments", ["project_id"]
    )


def downgrade():
    op.drop_table("execution_assignments")
    op.drop_table("project_worker_pools")
    with op.batch_alter_table("worker_instances") as batch:
        batch.drop_constraint("fk_worker_pool", type_="foreignkey")
        for name in ["registered_at", "capabilities", "pool_id", "protocol"]:
            batch.drop_column(name)
    op.drop_table("worker_pools")
