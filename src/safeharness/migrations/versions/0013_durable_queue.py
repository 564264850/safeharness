"""Opt-in durable submission; existing tasks are deliberately not enqueued."""

import sqlalchemy as sa
from alembic import op

revision = "0013_durable_queue"
down_revision = "0012_approval_loop"


def upgrade():
    op.create_table(
        "task_queue",
        sa.Column(
            "task_id",
            sa.CHAR(32),
            sa.ForeignKey("service_tasks.task_id"),
            primary_key=True,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.CheckConstraint("schema_version=1", name="queue_schema_version"),
        sa.CheckConstraint("state IN ('QUEUED','CANCELLED')", name="queue_state"),
    )
    op.create_index(
        "ix_task_queue_pending", "task_queue", ["state", "created_at", "task_id"]
    )
