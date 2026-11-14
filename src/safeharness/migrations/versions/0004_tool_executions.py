"""Add recoverable execution intents and control-plane receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0004_tool_executions"
down_revision = "0003_approval_decisions"


def upgrade() -> None:
    op.add_column(
        "pending_approvals",
        sa.Column(
            "policy_version",
            sa.String(),
            nullable=False,
            server_default="mock-ticket-v1",
        ),
    )
    with op.batch_alter_table(
        "tasks",
        recreate="always" if op.get_bind().dialect.name == "sqlite" else "never",
    ) as batch:
        batch.drop_constraint("taskstatus", type_="check")
        batch.create_check_constraint(
            "taskstatus",
            "status IN ('CREATED', 'RUNNING', 'WAITING_APPROVAL', "
            "'VERIFYING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'NEEDS_REVIEW')",
        )

    op.create_table(
        "tool_executions",
        sa.Column(
            "operation_id",
            sa.CHAR(32),
            sa.ForeignKey("approval_decisions.approval_id"),
            primary_key=True,
        ),
        sa.Column("workspace_id", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(9), nullable=False),
        sa.Column("ticket_revision", sa.Integer()),
        sa.Column("detail", sa.String()),
        sa.Column("claimed_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32)),
        sa.CheckConstraint(
            "status IN ('INTENT','SUCCEEDED','FAILED','CANCELLED','UNKNOWN')",
            name="execution_status",
        ),
    )
