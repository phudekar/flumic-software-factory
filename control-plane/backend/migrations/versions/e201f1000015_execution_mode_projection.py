"""Add an indexed-query-friendly mode projection without rewriting legacy pins."""
from alembic import op
import sqlalchemy as sa

revision = 'e201f1000015'
down_revision = 'e201f1000014'
branch_labels = depends_on = None


def upgrade():
    op.add_column('workflow_runs', sa.Column('execution_mode', sa.String(20),
        sa.CheckConstraint("execution_mode IN ('direct', 'protected')", name='ck_workflow_run_execution_mode'),
        nullable=True))


def downgrade():
    db = op.get_bind()
    if (db.scalar(sa.text("SELECT 1 FROM workflow_runs WHERE execution_mode='protected' LIMIT 1"))
            or db.scalar(sa.text("SELECT 1 FROM execution_policy_revisions WHERE default_mode='protected' OR minimum_mode='protected' LIMIT 1"))):
        # Check before any DDL, including when a downgrade continues to 0013.
        raise RuntimeError('Retain execution policy history or perform a verified policy rollback first')
    op.drop_column('workflow_runs', 'execution_mode')
