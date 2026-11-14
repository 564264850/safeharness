"""Successive approvals and resumable Agent approval sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0012_approval_loop"
down_revision = "0011_sessions"


def upgrade():
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("pending_approvals")
    name = next(c["name"] for c in constraints if c["column_names"] == ["task_id"])
    with op.batch_alter_table(
        "pending_approvals",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
    ) as batch:
        batch.drop_constraint(name or "uq_pending_approvals_task_id", type_="unique")
        batch.add_column(
            sa.Column("sequence", sa.Integer(), nullable=False, server_default="1")
        )
        batch.create_unique_constraint("uq_approval_sequence", ["task_id", "sequence"])
    op.add_column(
        "runtime_sessions",
        sa.Column(
            "approval_mode", sa.String(8), nullable=False, server_default="legacy"
        ),
    )
    op.add_column("runtime_sessions", sa.Column("approval_wait_started_at", sa.Float()))
