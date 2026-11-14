"""A shared, content-minimized Gateway invocation journal."""

import sqlalchemy as sa
from alembic import op

revision = "0005_gateway_events"
down_revision = "0004_tool_executions"


def upgrade():
    op.create_table(
        "gateway_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), nullable=False
        ),
        sa.Column("call_id", sa.CHAR(32), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.Integer()),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("arguments_sha256", sa.String(64)),
        sa.Column("resources", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.String(32), nullable=False),
    )
    op.create_index("ix_gateway_events_task", "gateway_events", ["task_id", "event_id"])
