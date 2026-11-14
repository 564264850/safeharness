"""Persist the tool request that caused a task to pause."""

import sqlalchemy as sa
from alembic import op

revision = "0002_pending_approvals"
down_revision = "0001_tasks"


def upgrade() -> None:
    with op.batch_alter_table(
        "tasks",
        recreate="always" if op.get_bind().dialect.name == "sqlite" else "never",
    ) as batch:
        batch.drop_constraint("taskstatus", type_="check")
        batch.alter_column("status", type_=sa.String(16))
        batch.create_check_constraint(
            "taskstatus",
            "status IN ('CREATED', 'RUNNING', 'WAITING_APPROVAL', "
            "'VERIFYING', 'SUCCEEDED', 'FAILED')",
        )

    op.create_table(
        "pending_approvals",
        sa.Column("approval_id", sa.CHAR(32), primary_key=True),
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), nullable=False
        ),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("canonical_arguments", sa.Text(), nullable=False),
        sa.UniqueConstraint("task_id"),
    )
