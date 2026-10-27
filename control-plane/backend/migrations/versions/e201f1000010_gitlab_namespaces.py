"""Allow nested GitLab namespaces without changing historical identity values."""

from alembic import op
import sqlalchemy as sa

revision = "e201f1000010"
down_revision = "e201f1000009"
branch_labels = depends_on = None


def upgrade():
    # SQLite does not enforce VARCHAR lengths. Avoid rebuilding this referenced
    # table there; PostgreSQL needs its actual enforced bound expanded.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "repository_connections",
            "owner",
            existing_type=sa.String(100),
            type_=sa.String(1024),
            existing_nullable=False,
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        oversized = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT count(*) FROM repository_connections WHERE length(owner) > 100"
                )
            )
            .scalar_one()
        )
        if oversized:
            raise RuntimeError(
                "Move long GitLab namespaces before downgrading; no data will be truncated"
            )
        op.alter_column(
            "repository_connections",
            "owner",
            existing_type=sa.String(1024),
            type_=sa.String(100),
            existing_nullable=False,
        )
