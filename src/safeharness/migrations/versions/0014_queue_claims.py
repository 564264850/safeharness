"""Short-transaction queue claims. No leases or automatic takeover yet."""

import sqlalchemy as sa
from alembic import op

revision = "0014_queue_claims"
down_revision = "0013_durable_queue"


def upgrade():
    with op.batch_alter_table("task_queue") as batch:
        batch.drop_constraint("queue_state", type_="check")
        batch.add_column(
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("claim_token", sa.String(32)))
        batch.add_column(sa.Column("claimed_by", sa.String(80)))
        batch.add_column(sa.Column("claimed_at", sa.String(32)))
        batch.add_column(sa.Column("finished_at", sa.String(32)))
        batch.create_check_constraint(
            "queue_state", "state IN ('QUEUED','CLAIMED','FINISHED','CANCELLED')"
        )
        batch.create_check_constraint("queue_generation", "generation>=0")
        batch.create_check_constraint(
            "queue_owner",
            "state NOT IN ('CLAIMED','FINISHED') OR "
            "(claim_token IS NOT NULL AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL AND generation>0)",
        )
