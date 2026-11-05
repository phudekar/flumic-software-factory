"""Add version-specific OpenBao credential references."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000013"
down_revision = "e201f1000012"
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table("secret_versions") as batch:
        batch.add_column(sa.Column("openbao_version", sa.Integer()))
        batch.alter_column("ciphertext", existing_type=sa.Text(), nullable=True)


def downgrade():
    db = op.get_bind()
    if db.scalar(sa.text(
        "SELECT 1 FROM secret_versions WHERE openbao_version IS NOT NULL LIMIT 1"
    )):
        raise RuntimeError("Remove OpenBao references through a verified rollback first")
    with op.batch_alter_table("secret_versions") as batch:
        batch.alter_column("ciphertext", existing_type=sa.Text(), nullable=False)
        batch.drop_column("openbao_version")
