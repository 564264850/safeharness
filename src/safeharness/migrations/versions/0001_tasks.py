"""Create the initial task table or adopt the matching M1.2 table."""

import sqlalchemy as sa
from alembic import op

revision = "0001_tasks"
down_revision = None

_STATUS_CHECK = "status IN ('CREATED', 'RUNNING', 'VERIFYING', 'SUCCEEDED', 'FAILED')"


def upgrade() -> None:
    schema = sa.inspect(op.get_bind())
    if schema.has_table("tasks"):
        columns = [
            (column["name"], str(column["type"]), column["nullable"])
            for column in schema.get_columns("tasks")
        ]
        primary_key = schema.get_pk_constraint("tasks")["constrained_columns"]
        checks = schema.get_check_constraints("tasks")
        if (
            columns
            != [
                ("task_id", "CHAR(32)", False),
                ("status", "VARCHAR(9)", False),
                ("revision", "INTEGER", False),
            ]
            or primary_key != ["task_id"]
            or checks != [{"name": "taskstatus", "sqltext": _STATUS_CHECK}]
        ):
            raise RuntimeError("Existing tasks table does not match the M1.2 schema")
        return

    op.create_table(
        "tasks",
        sa.Column("task_id", sa.CHAR(32), primary_key=True),
        sa.Column("status", sa.String(9), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(_STATUS_CHECK, name="taskstatus"),
    )
