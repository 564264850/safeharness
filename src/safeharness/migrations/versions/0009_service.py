"""Task ownership and the PostgreSQL mock business service."""

import sqlalchemy as sa
from alembic import op

revision = "0009_service"
down_revision = "0008_evaluation_reports"


def upgrade():
    op.create_table(
        "service_tasks",
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), primary_key=True
        ),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "mock_workspaces",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.String(32), nullable=False),
    )
    op.create_table(
        "mock_tickets",
        sa.Column("ticket_id", sa.String(16), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open','in_progress','resolved')", name="mock_ticket_status"
        ),
    )
    op.create_table(
        "mock_receipts",
        sa.Column("operation_id", sa.String(32), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("ticket_id", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
