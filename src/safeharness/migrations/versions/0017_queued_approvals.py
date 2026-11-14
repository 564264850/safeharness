"""Park approval waits and durably wake approved continuations."""

from alembic import op

revision = "0017_queued_approvals"
down_revision = "0016_queue_recovery"


def upgrade():
    with op.batch_alter_table("task_queue") as batch:
        batch.drop_constraint("queue_state", type_="check")
        batch.drop_constraint("queue_recovery_mode", type_="check")
        batch.create_check_constraint(
            "queue_state",
            "state IN ('QUEUED','READY','WAITING','CLAIMED','FINISHED','CANCELLED')",
        )
        batch.create_check_constraint(
            "queue_recovery_mode",
            "recovery_mode IS NULL OR recovery_mode IN ('start','initialize','resume','approval','review','failed','terminal')",
        )
