"""Preserve the Coding demonstration and independent verification result."""

import sqlalchemy as sa
from alembic import op

revision = "0006_coding_reports"
down_revision = "0005_gateway_events"


def upgrade():
    op.create_table(
        "coding_reports",
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), primary_key=True
        ),
        sa.Column("report_json", sa.Text(), nullable=False),
    )
