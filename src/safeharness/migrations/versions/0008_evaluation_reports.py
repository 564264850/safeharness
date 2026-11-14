"""Keep public evaluation outcomes and local controlled observation records."""

import sqlalchemy as sa
from alembic import op

revision = "0008_evaluation_reports"
down_revision = "0007_trace_events"


def upgrade():
    op.create_table(
        "evaluation_reports",
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), primary_key=True
        ),
        sa.Column("public_json", sa.Text(), nullable=False),
        sa.Column("private_json", sa.Text(), nullable=False),
    )
