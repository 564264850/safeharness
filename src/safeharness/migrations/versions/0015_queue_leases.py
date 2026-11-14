"""Database-time leases; historical claims expire without being requeued."""

import sqlalchemy as sa
from alembic import op

revision = "0015_queue_leases"
down_revision = "0014_queue_claims"


def upgrade():
    op.add_column("task_queue", sa.Column("lease_seconds", sa.Float()))
    op.add_column("task_queue", sa.Column("last_heartbeat_at", sa.Float()))
    op.add_column("task_queue", sa.Column("lease_expires_at", sa.Float()))
    op.add_column(
        "task_queue",
        sa.Column("heartbeat_count", sa.Integer(), nullable=False, server_default="0"),
    )
    bind = op.get_bind()
    clock = (
        "EXTRACT(EPOCH FROM clock_timestamp())"
        if bind.dialect.name == "postgresql"
        else "(julianday('now')-2440587.5)*86400.0"
    )
    # Do not invent a fresh execution right for a pre-lease, potentially lost owner.
    bind.execute(
        sa.text(
            f"UPDATE task_queue SET lease_seconds=30,last_heartbeat_at={clock},lease_expires_at={clock} WHERE state='CLAIMED'"
        )
    )
    with op.batch_alter_table("task_queue") as batch:
        batch.create_check_constraint(
            "queue_lease_duration",
            "lease_seconds IS NULL OR (lease_seconds>=0.5 AND lease_seconds<=3600)",
        )
        batch.create_check_constraint("queue_heartbeat_count", "heartbeat_count>=0")
        batch.create_check_constraint(
            "queue_claim_lease",
            "state!='CLAIMED' OR (lease_seconds IS NOT NULL AND last_heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
        )
