"""Private runtime snapshots with generation ownership and immutable checkpoints."""

import sqlalchemy as sa
from alembic import op

revision = "0011_sessions"
down_revision = "0010_invocations"


def upgrade():
    op.create_table(
        "runtime_sessions",
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), primary_key=True
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("config_sha256", sa.CHAR(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.CHAR(32)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("checkpoint_id", sa.CHAR(32)),
        sa.Column("deadline_at", sa.Float(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.CheckConstraint("generation>0 AND max_steps>0", name="session_limits"),
        sa.CheckConstraint(
            "state IN ('RUNNING','PAUSED','FINISHED','WAITING_APPROVAL','NEEDS_REVIEW')",
            name="session_state",
        ),
    )
    op.create_table(
        "runtime_checkpoints",
        sa.Column("checkpoint_id", sa.CHAR(32), primary_key=True),
        sa.Column(
            "task_id",
            sa.CHAR(32),
            sa.ForeignKey("runtime_sessions.task_id"),
            nullable=False,
        ),
        sa.Column(
            "parent_id", sa.CHAR(32), sa.ForeignKey("runtime_checkpoints.checkpoint_id")
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "run_id", sa.CHAR(32), sa.ForeignKey("task_runs.run_id"), nullable=False
        ),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.UniqueConstraint("task_id", "sequence"),
        sa.CheckConstraint(
            "byte_count>=0 AND byte_count<=2097152", name="checkpoint_bytes"
        ),
        sa.CheckConstraint(
            "phase IN ('initial','before_model','after_model','before_tool','after_tool','final')",
            name="checkpoint_phase",
        ),
    )
