"""Runtime target pins, remote resource identities and shared controller leases."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000012"
down_revision = "e201f1000011"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "worker_instances",
        sa.Column("target_snapshot", sa.JSON(), nullable=False, server_default="{}"),
    )
    for column in (
        sa.Column(
            "target_id", sa.String(80), nullable=False, server_default="local-docker"
        ),
        sa.Column("target_revision", sa.String(64), nullable=False, server_default=""),
        sa.Column("target_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "runtime_phase", sa.String(24), nullable=False, server_default="intent"
        ),
        sa.Column(
            "cleanup_pending", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    ):
        op.add_column("runtime_jobs", column)
    with op.batch_alter_table("runtime_jobs") as batch:
        batch.create_unique_constraint(
            "uq_runtime_job_project_identity", ["project_id", "id"]
        )
    op.create_table(
        "runtime_leases",
        sa.Column("resource_key", sa.String(160), primary_key=True),
        sa.Column("owner", sa.String(36), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "runtime_resource_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(80), nullable=False),
        sa.Column("target_revision", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("logical_id", sa.String(160), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("name", sa.String(253), nullable=False),
        sa.Column("uid", sa.String(128), nullable=False),
        sa.Column("owner_uid", sa.String(128)),
        sa.Column("spec_digest", sa.String(64), nullable=False),
        sa.Column("pending_spec_digest", sa.String(64)),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id")),
        sa.Column("worker_id", sa.String(36), sa.ForeignKey("worker_instances.id")),
        sa.Column("job_id", sa.String(64)),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "target_id",
            "target_revision",
            "kind",
            "logical_id",
            name="uq_runtime_binding_logical",
        ),
        sa.UniqueConstraint(
            "target_id",
            "target_revision",
            "namespace",
            "kind",
            "uid",
            name="uq_runtime_binding_remote",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "job_id"],
            ["runtime_jobs.project_id", "runtime_jobs.id"],
            name="fk_runtime_binding_job",
        ),
        sa.CheckConstraint(
            "(worker_id IS NOT NULL AND job_id IS NULL AND project_id IS NULL) OR (worker_id IS NULL AND job_id IS NOT NULL AND project_id IS NOT NULL)",
            name="ck_runtime_binding_owner",
        ),
    )


def downgrade():
    bind = op.get_bind()
    # Encrypted launches cannot be silently rewritten by a schema downgrade.
    # Retain authority/tombstones until an operator has reconciled and archived
    # all new pins, even if their last observed application state is terminal.
    checks = (
        "SELECT 1 FROM worker_instances WHERE target_id <> 'local-docker' OR CAST(target_snapshot AS TEXT) <> '{}' LIMIT 1",
        "SELECT 1 FROM runtime_jobs WHERE target_id <> 'local-docker' OR target_revision <> '' OR CAST(target_snapshot AS TEXT) <> '{}' LIMIT 1",
        "SELECT 1 FROM runtime_resource_bindings LIMIT 1",
        "SELECT 1 FROM runtime_leases WHERE owner IS NOT NULL LIMIT 1",
    )
    if any(bind.execute(sa.text(query)).first() is not None for query in checks):
        raise RuntimeError(
            "Reconcile and archive pinned runtime authority before downgrade"
        )
    op.drop_table("runtime_resource_bindings")
    op.drop_table("runtime_leases")
    with op.batch_alter_table("runtime_jobs") as batch:
        batch.drop_constraint("uq_runtime_job_project_identity", type_="unique")
        for name in (
            "cleanup_pending",
            "runtime_phase",
            "target_snapshot",
            "target_revision",
            "target_id",
        ):
            batch.drop_column(name)
    op.drop_column("worker_instances", "target_snapshot")
