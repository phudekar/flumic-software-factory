"""Namespace workflow-control idempotency keys by owning project."""
from alembic import op
import sqlalchemy as sa

revision = "e201f1000002"
down_revision = "e201f1000001"
branch_labels = depends_on = None


def upgrade():
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("workflow_control_operations")
    old = next(item for item in constraints if item["column_names"] == ["key"])
    with op.batch_alter_table("workflow_control_operations", naming_convention={"uq":"uq_%(table_name)s_%(column_0_name)s"}) as batch:
        batch.drop_constraint(old["name"] or "uq_workflow_control_operations_key", type_="unique")
        batch.create_unique_constraint("uq_workflow_control_project_key", ["project_id", "key"])


def downgrade():
    duplicates = op.get_bind().execute(sa.text('SELECT "key" FROM workflow_control_operations GROUP BY "key" HAVING COUNT(*) > 1')).first()
    if duplicates:
        raise RuntimeError("Cannot restore global idempotency keys while projects have duplicate keys")
    with op.batch_alter_table("workflow_control_operations") as batch:
        batch.drop_constraint("uq_workflow_control_project_key", type_="unique")
        batch.create_unique_constraint("uq_workflow_control_operations_key", ["key"])
