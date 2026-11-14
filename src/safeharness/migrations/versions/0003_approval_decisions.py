"""Record a single operator decision for each approval request."""

import sqlalchemy as sa
from alembic import op

revision = "0003_approval_decisions"
down_revision = "0002_pending_approvals"


def upgrade() -> None:
    op.create_table(
        "approval_decisions",
        sa.Column(
            "approval_id",
            sa.CHAR(32),
            sa.ForeignKey("pending_approvals.approval_id"),
            primary_key=True,
        ),
        sa.Column("outcome", sa.String(8), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('APPROVED', 'REJECTED')", name="approval_outcome"
        ),
    )
