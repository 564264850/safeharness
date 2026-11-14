"""Separate task/run/call/attempt identity, private artifacts and submission keys."""

import sqlalchemy as sa
from alembic import op

revision = "0010_invocations"
down_revision = "0009_service"


def upgrade():
    op.create_table(
        "task_runs",
        sa.Column("run_id", sa.CHAR(32), primary_key=True),
        sa.Column(
            "task_id", sa.CHAR(32), sa.ForeignKey("tasks.task_id"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32)),
        sa.UniqueConstraint("run_id", "task_id"),
        sa.CheckConstraint(
            "kind IN ('agent','business','control','provider')", name="run_kind"
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED','NEEDS_REVIEW','UNKNOWN')",
            name="run_status",
        ),
    )
    predicate = sa.text("status='RUNNING' AND kind<>'provider'")
    op.create_index(
        "uq_active_execution_run",
        "task_runs",
        ["task_id"],
        unique=True,
        sqlite_where=predicate,
        postgresql_where=predicate,
    )
    op.create_table(
        "logical_calls",
        sa.Column("call_id", sa.CHAR(32), primary_key=True),
        sa.Column("task_id", sa.CHAR(32), nullable=False),
        sa.Column("run_id", sa.CHAR(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(24), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.Integer()),
        sa.Column("policy_version", sa.String(64)),
        sa.Column("request_sha256", sa.CHAR(64)),
        sa.Column("arguments_sha256", sa.CHAR(64)),
        sa.Column("operation_id", sa.CHAR(32), unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32)),
        sa.UniqueConstraint("call_id", "task_id"),
        sa.UniqueConstraint("run_id", "ordinal"),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"], ["task_runs.run_id", "task_runs.task_id"]
        ),
        sa.CheckConstraint("ordinal>0", name="call_ordinal"),
        sa.CheckConstraint("kind IN ('tool','model','verification')", name="call_kind"),
        sa.CheckConstraint(
            "status IN ('OPEN','WAITING_APPROVAL','SUCCEEDED','FAILED','DENIED','UNKNOWN','CANCELLED')",
            name="call_status",
        ),
    )
    op.create_table(
        "call_attempts",
        sa.Column("attempt_id", sa.CHAR(32), primary_key=True),
        sa.Column("task_id", sa.CHAR(32), nullable=False),
        sa.Column("run_id", sa.CHAR(32), nullable=False),
        sa.Column("call_id", sa.CHAR(32), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32)),
        sa.UniqueConstraint("call_id", "attempt_index"),
        sa.UniqueConstraint("attempt_id", "task_id", "run_id", "call_id"),
        sa.ForeignKeyConstraint(
            ["run_id", "task_id"], ["task_runs.run_id", "task_runs.task_id"]
        ),
        sa.ForeignKeyConstraint(
            ["call_id", "task_id"], ["logical_calls.call_id", "logical_calls.task_id"]
        ),
        sa.CheckConstraint("attempt_index>0", name="attempt_index"),
        sa.CheckConstraint(
            "kind IN ('tool','proposal','business','model','verification')",
            name="attempt_kind",
        ),
        sa.CheckConstraint(
            "status IN ('INTENT','SUCCEEDED','FAILED','DENIED','WAITING_APPROVAL','UNKNOWN','REUSED','CANCELLED','RECONCILED')",
            name="attempt_status",
        ),
    )
    op.create_table(
        "call_artifacts",
        sa.Column("artifact_id", sa.CHAR(32), primary_key=True),
        sa.Column("task_id", sa.CHAR(32), nullable=False),
        sa.Column("run_id", sa.CHAR(32), nullable=False),
        sa.Column("call_id", sa.CHAR(32), nullable=False),
        sa.Column("attempt_id", sa.CHAR(32), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.UniqueConstraint("attempt_id", "kind"),
        sa.ForeignKeyConstraint(
            ["attempt_id", "task_id", "run_id", "call_id"],
            [
                "call_attempts.attempt_id",
                "call_attempts.task_id",
                "call_attempts.run_id",
                "call_attempts.call_id",
            ],
        ),
        sa.CheckConstraint(
            "byte_count>=0 AND byte_count<=262144", name="artifact_bytes"
        ),
    )
    op.create_table(
        "approval_calls",
        sa.Column(
            "approval_id",
            sa.CHAR(32),
            sa.ForeignKey("pending_approvals.approval_id"),
            primary_key=True,
        ),
        sa.Column("task_id", sa.CHAR(32), nullable=False),
        sa.Column("call_id", sa.CHAR(32), nullable=False, unique=True),
        sa.ForeignKeyConstraint(
            ["call_id", "task_id"], ["logical_calls.call_id", "logical_calls.task_id"]
        ),
    )
    op.create_table(
        "task_submissions",
        sa.Column("owner_id", sa.String(80), primary_key=True),
        sa.Column("key_sha256", sa.CHAR(64), primary_key=True),
        sa.Column("request_sha256", sa.CHAR(64), nullable=False),
        sa.Column(
            "task_id",
            sa.CHAR(32),
            sa.ForeignKey("tasks.task_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("created_at", sa.String(32), nullable=False),
    )
    op.add_column("trace_events", sa.Column("run_id", sa.CHAR(32)))
    op.add_column("gateway_events", sa.Column("run_id", sa.CHAR(32)))
    op.add_column("gateway_events", sa.Column("attempt_id", sa.CHAR(32)))
    op.create_index(
        "ux_gateway_attempt_phase",
        "gateway_events",
        ["attempt_id", "phase"],
        unique=True,
    )
    # Historical rows deliberately keep NULL identity; no fabricated run history.
