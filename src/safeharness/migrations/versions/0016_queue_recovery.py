"""Durable recovery decisions and pinned continuation checkpoints."""

import sqlalchemy as sa
from alembic import op

revision = "0016_queue_recovery"
down_revision = "0015_queue_leases"


def upgrade():
    with op.batch_alter_table("task_queue") as batch:
        batch.drop_constraint("queue_state", type_="check")
        batch.add_column(
            sa.Column(
                "recovery_count", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(sa.Column("recovery_mode", sa.String(16)))
        batch.add_column(sa.Column("recovery_code", sa.String(64)))
        batch.add_column(sa.Column("recovery_checkpoint_id", sa.CHAR(32)))
        batch.add_column(sa.Column("resume_generation", sa.Integer()))
        batch.add_column(sa.Column("execution_deadline_at", sa.Float()))
        batch.create_check_constraint(
            "queue_state",
            "state IN ('QUEUED','READY','CLAIMED','FINISHED','CANCELLED')",
        )
        batch.create_check_constraint("queue_recoveries", "recovery_count>=0")
        batch.create_check_constraint(
            "queue_recovery_mode",
            "recovery_mode IS NULL OR recovery_mode IN ('start','initialize','resume','review','failed','terminal')",
        )
    op.execute(
        "UPDATE task_queue SET execution_deadline_at=(SELECT deadline_at FROM runtime_sessions s WHERE s.task_id=task_queue.task_id)"
    )
