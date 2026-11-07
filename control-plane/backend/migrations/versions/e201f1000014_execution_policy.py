"""Versioned project execution policies; missing heads retain immutable direct-v1."""
from alembic import op
import sqlalchemy as sa

revision = 'e201f1000014'
down_revision = 'e201f1000013'
branch_labels = depends_on = None


def upgrade():
    op.create_table('execution_policy_revisions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('default_mode', sa.String(20), nullable=False),
        sa.Column('minimum_mode', sa.String(20), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('project_id', 'id', name='uq_execution_policy_owner'),
        sa.CheckConstraint("default_mode IN ('direct', 'protected')", name='ck_execution_policy_default'),
        sa.CheckConstraint("minimum_mode IN ('direct', 'protected')", name='ck_execution_policy_minimum'),
        sa.CheckConstraint("minimum_mode != 'protected' OR default_mode = 'protected'", name='ck_execution_policy_strength'),
    )
    op.create_index('ix_execution_policy_revisions_project_id', 'execution_policy_revisions', ['project_id'])
    op.create_table('execution_policy_heads',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), primary_key=True),
        sa.Column('revision_id', sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(['project_id', 'revision_id'],
            ['execution_policy_revisions.project_id', 'execution_policy_revisions.id'],
            name='fk_execution_policy_head_owner'),
    )


def downgrade():
    # Losing protected policy on downgrade would silently weaken future admission.
    db = op.get_bind()
    if db.scalar(sa.text("SELECT 1 FROM execution_policy_revisions WHERE default_mode = 'protected' OR minimum_mode = 'protected' LIMIT 1")):
        raise RuntimeError('Retain execution policy history or perform a verified policy rollback first')
    op.drop_table('execution_policy_heads')
    op.drop_index('ix_execution_policy_revisions_project_id', table_name='execution_policy_revisions')
    op.drop_table('execution_policy_revisions')
