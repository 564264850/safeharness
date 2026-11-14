"""Unify state, Gateway and model events without exporting raw content."""

import sqlalchemy as sa
from alembic import op

revision = "0007_trace_events"
down_revision = "0006_coding_reports"


def upgrade():
    op.create_table(
        "trace_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), nullable=False
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.String(32), nullable=False),
    )
    op.create_index("ix_trace_task", "trace_events", ["task_id", "sequence"])
