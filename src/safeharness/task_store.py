"""Persist the latest committed task state in SQLite."""

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    inspect,
    literal,
    select,
)
from sqlalchemy.engine import Connection

from .approval import (
    ApprovalActor,
    ApprovalDecision,
    ApprovalIntent,
    ApprovalOutcome,
    ApprovalRecord,
    PendingApproval,
)
from .contracts import CallIdentity, SubmissionConflictError
from .database import (
    database_exists,
    engine_for,
    execute,
    lock_task,
    normalize_database,
)
from .execution import (
    TICKET_POLICY_VERSION,
    UpdateTicketStatusArgs,
    verify_update_intent,
)
from .failures import RuntimeFailure
from .invocations import (
    approval_call,
    close_runs,
    digest,
    ensure_run,
    finish_attempt,
    identity_for_attempt,
    link_approval,
    new_attempt,
    now,
)
from .scheduling import (
    QueueClaimError,
    cancel_queued,
    canonical_request,
    clock_sql,
    enqueue,
    finish_claim,
    public_scheduling,
    require_claim,
    scheduling,
    wake_approval,
)
from .task import Task, TaskStatus
from .ticket_store import EvidenceUnavailable, TicketStore
from .trace import insert_event

_MIGRATION_LOCK = Lock()
_TASKS = Table(
    "tasks",
    MetaData(),
    Column("task_id", Uuid(native_uuid=False), primary_key=True),
    Column(
        "status",
        Enum(TaskStatus, native_enum=False, create_constraint=True),
        nullable=False,
    ),
    Column("revision", Integer, nullable=False),
)
_PENDING_APPROVALS = Table(
    "pending_approvals",
    _TASKS.metadata,
    Column("approval_id", Uuid(native_uuid=False), primary_key=True),
    Column(
        "task_id",
        Uuid(native_uuid=False),
        ForeignKey("tasks.task_id"),
        nullable=False,
    ),
    Column("tool_name", String, nullable=False),
    Column("tool_version", Integer, nullable=False),
    Column("canonical_arguments", Text, nullable=False),
    Column("policy_version", String, nullable=False),
    Column("sequence", Integer, nullable=False, server_default="1"),
)
_APPROVAL_DECISIONS = Table(
    "approval_decisions",
    _TASKS.metadata,
    Column(
        "approval_id",
        Uuid(native_uuid=False),
        ForeignKey("pending_approvals.approval_id"),
        primary_key=True,
    ),
    Column(
        "outcome",
        Enum(
            ApprovalOutcome,
            native_enum=False,
            create_constraint=True,
            name="approval_outcome",
        ),
        nullable=False,
    ),
    Column("decided_by", String, nullable=False),
    Column("arguments_sha256", String(64), nullable=False),
    Column("decided_at", String(32), nullable=False),
)
_EXECUTIONS = Table(
    "tool_executions",
    _TASKS.metadata,
    Column(
        "operation_id",
        Uuid(native_uuid=False),
        ForeignKey("approval_decisions.approval_id"),
        primary_key=True,
    ),
    Column("workspace_id", String(32), nullable=False),
    Column("policy_version", String, nullable=False),
    Column("tool_name", String, nullable=False),
    Column("tool_version", Integer, nullable=False),
    Column("arguments_sha256", String(64), nullable=False),
    Column("status", String(9), nullable=False),
    Column("ticket_revision", Integer),
    Column("detail", String),
    Column("claimed_at", String(32), nullable=False),
    Column("finished_at", String(32)),
)
_APPROVAL_TTL = timedelta(hours=24)
_CODING_REPORTS = Table(
    "coding_reports",
    _TASKS.metadata,
    Column(
        "task_id",
        Uuid(native_uuid=False),
        ForeignKey("tasks.task_id"),
        primary_key=True,
    ),
    Column("report_json", Text, nullable=False),
)
_EVALUATION_REPORTS = Table(
    "evaluation_reports",
    _TASKS.metadata,
    Column(
        "task_id",
        Uuid(native_uuid=False),
        ForeignKey("tasks.task_id"),
        primary_key=True,
    ),
    Column("public_json", Text, nullable=False),
    Column("private_json", Text, nullable=False),
)
_SERVICE_TASKS = Table(
    "service_tasks",
    _TASKS.metadata,
    Column(
        "task_id",
        Uuid(native_uuid=False),
        ForeignKey("tasks.task_id"),
        primary_key=True,
    ),
    Column("owner_id", String(80), nullable=False),
    Column("request_json", Text, nullable=False),
)


class TaskConflictError(RuntimeError):
    """The task snapshot no longer matches the database."""


class ApprovalConflictError(RuntimeError):
    """The selected request is missing, changed, or already decided."""


class ExecutionConflictError(RuntimeError):
    """The requested state change is blocked by policy or execution state."""


class TaskStore:
    def __init__(self, database: Path):
        self._database = normalize_database(database)
        self._engine = engine_for(self._database)

    @property
    def database(self) -> Path:
        return self._database

    def initialize(self) -> None:
        if isinstance(self._database, Path):
            self._database.parent.mkdir(parents=True, exist_ok=True)
        with _MIGRATION_LOCK, self._engine.begin() as connection:
            # SQLite table-copy migrations may rebuild a referenced parent table.
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(734271901)")
            config = Config(attributes={"connection": connection})
            config.set_main_option("script_location", "safeharness:migrations")
            command.upgrade(config, "head")
            if connection.dialect.name == "sqlite" and (
                connection.exec_driver_sql("PRAGMA foreign_key_check").first()
                is not None
            ):
                raise RuntimeError("Migration would leave invalid foreign keys")

    def create(
        self, *, owner_id: str | None = None, request: dict | None = None
    ) -> Task:
        if (owner_id is None) != (request is None):
            raise ValueError("Task owner and service request must be recorded together")
        with self._engine.begin() as connection:
            return self._create(connection, owner_id, request)

    @staticmethod
    def _create(connection, owner_id=None, request=None):
        if owner_id is not None:
            request = canonical_request(request)
        task = Task()
        connection.execute(
            _TASKS.insert().values(
                task_id=task.task_id, status=task.status, revision=task.revision
            )
        )
        insert_event(
            connection,
            task.task_id,
            "task.created",
            {"status": task.status.value, "revision": task.revision},
        )
        if owner_id is not None:
            connection.execute(
                _SERVICE_TASKS.insert().values(
                    task_id=task.task_id,
                    owner_id=owner_id,
                    request_json=json.dumps(request),
                )
            )
            if request.get("execution_mode") == "queued":
                enqueue(connection, task.task_id)
        return task

    def submit(
        self,
        owner_id: str,
        request: dict,
        idempotency_key: str | None = None,
        *,
        max_pending=1000,
    ):
        if (
            not isinstance(owner_id, str)
            or not 1 <= len(owner_id) <= 80
            or not owner_id.strip()
            or not isinstance(request, dict)
        ):
            raise ValueError("Invalid submission owner or request")
        request = canonical_request(request)
        if idempotency_key is None:
            if request.get("execution_mode") == "queued":
                from .operations import admit

                with self._engine.begin() as connection:
                    if connection.dialect.name == "sqlite":
                        connection.exec_driver_sql("BEGIN IMMEDIATE")
                    admit(connection, max_pending)
                    return self._create(connection, owner_id, request), False
            return self.create(owner_id=owner_id, request=request), False
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise ValueError("Invalid idempotency key")
        key_sha = hashlib.sha256(idempotency_key.encode()).hexdigest()
        request_sha = digest(request)
        with self._engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                lock_id = int.from_bytes(
                    hashlib.sha256((owner_id + "\0" + key_sha).encode()).digest()[:8],
                    signed=True,
                )
                execute(connection, "SELECT pg_advisory_xact_lock(?)", (lock_id,))
            old = execute(
                connection,
                "SELECT request_sha256,task_id FROM task_submissions WHERE owner_id=? AND key_sha256=?",
                (owner_id, key_sha),
            ).fetchone()
            if old:
                if old[0] != request_sha:
                    raise SubmissionConflictError("Idempotency key payload mismatch")
                return self._require_task(connection, UUID(old[1])), True
            if request.get("execution_mode") == "queued":
                from .operations import admit

                admit(connection, max_pending)
            task = self._create(connection, owner_id, request)
            execute(
                connection,
                "INSERT INTO task_submissions VALUES (?,?,?,?,?)",
                (owner_id, key_sha, request_sha, task.task_id.hex, now()),
            )
            return task, False

    def scheduling(self, task_id: UUID):
        with self._engine.connect() as connection:
            return scheduling(connection, task_id)

    def task_with_scheduling(self, task_id: UUID):
        """One statement prevents mixing pre-cancel task state with post-cancel queue state."""
        with self._engine.connect() as connection:
            row = (
                execute(
                    connection,
                    f"SELECT t.status,t.revision,q.schema_version,q.state,q.created_at,q.generation,q.claimed_by,q.claimed_at,q.finished_at,q.lease_seconds,q.last_heartbeat_at,q.lease_expires_at,q.heartbeat_count,q.recovery_count,q.recovery_mode,q.recovery_code,q.execution_deadline_at,q.lease_expires_at>{clock_sql(connection)} AS lease_valid "
                    "FROM tasks t LEFT JOIN task_queue q ON t.task_id=q.task_id WHERE t.task_id=?",
                    (task_id.hex,),
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None, None
        return Task(
            task_id, TaskStatus(row["status"]), row["revision"]
        ), public_scheduling(row)

    def service_task(self, task_id: UUID):
        with self._engine.connect() as connection:
            row = connection.execute(
                select(_SERVICE_TASKS).where(_SERVICE_TASKS.c.task_id == task_id)
            ).one_or_none()
        return (
            {"owner_id": row.owner_id, "request": json.loads(row.request_json)}
            if row
            else None
        )

    def approval_task_id(self, approval_id: UUID):
        with self._engine.connect() as connection:
            return connection.execute(
                select(_PENDING_APPROVALS.c.task_id).where(
                    _PENDING_APPROVALS.c.approval_id == approval_id
                )
            ).scalar_one_or_none()

    def get(self, task_id: UUID) -> Task | None:
        if not database_exists(self._database):
            return None

        with self._engine.connect() as connection:
            return self._read_task(connection, task_id)

    def complete_coding(self, task: Task, report: dict) -> Task:
        if task.status is not TaskStatus.VERIFYING or report["task_id"] != str(
            task.task_id
        ):
            raise ValueError("A Coding report must finalize its VERIFYING task")
        target = (
            TaskStatus.SUCCEEDED
            if report["verification"]["passed"] is True
            else TaskStatus.FAILED
        )
        if report["task_status"] != target.value:
            raise ValueError("Report outcome does not match the task outcome")
        finished = task.transition(target)
        with self._engine.begin() as connection:
            self._update_task(connection, task, finished)
            connection.execute(
                _CODING_REPORTS.insert().values(
                    task_id=task.task_id, report_json=json.dumps(report)
                )
            )
        return finished

    def reject_queued_approval(self, expected: Task, code: str) -> Task:
        """Fail closed when a claimed approval continuation cannot be initialized."""
        failure = RuntimeFailure("checkpoint", code, True)
        with self._engine.begin() as connection:
            lock_task(connection, expected.task_id)
            claim = require_claim(connection, expected.task_id)
            current = self._require_task(connection, expected.task_id)
            if (
                claim is None
                or claim.recovery_mode != "approval"
                or current != expected
                or current.status is not TaskStatus.WAITING_APPROVAL
            ):
                raise TaskConflictError("Approval continuation changed")
            running = current.transition(TaskStatus.RUNNING)
            self._update_task(connection, current, running)
            stopped = running.transition(TaskStatus.NEEDS_REVIEW)
            self._update_task(connection, running, stopped)
            execute(
                connection,
                "UPDATE runtime_sessions SET state='NEEDS_REVIEW',owner_token=NULL WHERE task_id=?",
                (expected.task_id.hex,),
            )
            insert_event(
                connection,
                expected.task_id,
                "runtime.error",
                {
                    "phase": failure.phase,
                    "code": failure.code,
                    "outcome_unknown": True,
                    "recorded_status": stopped.status.value,
                    "recovery_action": "manual_review",
                },
            )
            return stopped

    def record_runtime_failure(self, expected: Task, failure: RuntimeFailure) -> Task:
        """Atomically converge a live runner; never retry or dispatch operations."""
        with self._engine.begin() as connection:
            lock_task(connection, expected.task_id)
            current = self._require_task(connection, expected.task_id)
            execution = self._read_execution(connection, expected.task_id)
            if current.status is TaskStatus.WAITING_APPROVAL:
                recovery = "review_approval"
            elif current.status not in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                recovery = "manual_review" if failure.outcome_unknown else "none"
            elif execution is not None and execution.status in {"INTENT", "UNKNOWN"}:
                recovery = "reconcile_business_receipt"
            elif current != expected:
                recovery = "inspect_current_revision"
            else:
                target = (
                    TaskStatus.NEEDS_REVIEW
                    if failure.outcome_unknown
                    else TaskStatus.FAILED
                )
                updated = current.transition(target)
                self._update_task(connection, current, updated)
                current = updated
                recovery = "manual_review" if failure.outcome_unknown else "none"
            insert_event(
                connection,
                current.task_id,
                "runtime.error",
                {
                    "phase": failure.phase,
                    "code": failure.code,
                    "outcome_unknown": failure.outcome_unknown,
                    "recorded_status": current.status.value,
                    "recovery_action": recovery,
                },
            )
        return current

    def get_runtime_failure(self, task_id: UUID) -> dict | None:
        if not database_exists(self._database):
            return None
        with self._engine.connect() as connection:
            if not inspect(connection).has_table("trace_events"):
                return None
            row = execute(
                connection,
                "SELECT payload_json FROM trace_events WHERE task_id=? AND kind='runtime.error' ORDER BY sequence DESC LIMIT 1",
                (task_id.hex,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def record_evaluation(
        self, task: Task, report: dict, private: dict, *, finalize=False
    ) -> Task:
        finished = task
        if finalize:
            if task.status is not TaskStatus.VERIFYING:
                raise ValueError("Evaluation finalization requires VERIFYING")
            finished = task.transition(
                TaskStatus.SUCCEEDED if report["contract_passed"] else TaskStatus.FAILED
            )
        if (
            report["task_id"] != str(task.task_id)
            or report["task_status"] != finished.status.value
        ):
            raise ValueError("Evaluation report does not match its task")
        with self._engine.begin() as connection:
            if finalize:
                self._update_task(connection, task, finished)
            elif self._require_task(connection, task.task_id) != task:
                raise TaskConflictError("Task changed before evaluation persistence")
            connection.execute(
                _EVALUATION_REPORTS.insert().values(
                    task_id=task.task_id,
                    public_json=json.dumps(report),
                    private_json=json.dumps(private),
                )
            )
            insert_event(
                connection,
                task.task_id,
                "verification",
                {
                    "passed": report["contract_passed"],
                    "code": report["verification_code"],
                    "case_count": report.get("verification_case_count", 1),
                    "verifier_version": report["verifier_version"],
                },
            )
        return finished

    def transition(self, task: Task, status: TaskStatus) -> Task:
        if status is TaskStatus.WAITING_APPROVAL:
            raise ValueError("Use pause_for_approval to persist the approval request")
        if task.status is TaskStatus.WAITING_APPROVAL:
            raise ValueError("Use the approval, execution or cancellation workflow")
        if status in (TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW):
            raise ValueError("Use the execution or cancellation workflow")
        updated = task.transition(status)
        with self._engine.begin() as connection:
            self._update_task(connection, task, updated)
        return updated

    def pause_for_approval(
        self, task: Task, intent: ApprovalIntent, call_id: UUID | None = None
    ) -> tuple[Task, PendingApproval]:
        waiting = task.transition(TaskStatus.WAITING_APPROVAL)
        approval = PendingApproval(uuid4(), task.task_id, intent, TICKET_POLICY_VERSION)
        with self._engine.begin() as connection:
            lock_task(connection, task.task_id)
            sequence = execute(
                connection,
                "SELECT coalesce(max(sequence),0)+1 FROM pending_approvals WHERE task_id=?",
                (task.task_id.hex,),
            ).scalar_one()
            connection.execute(
                _PENDING_APPROVALS.insert().values(
                    approval_id=approval.approval_id,
                    task_id=approval.task_id,
                    tool_name=intent.tool_name,
                    tool_version=intent.tool_version,
                    canonical_arguments=intent.canonical_arguments,
                    policy_version=approval.policy_version,
                    sequence=sequence,
                )
            )
            link_approval(
                connection, task.task_id, approval.approval_id, intent, call_id
            )
            self._update_task(connection, task, waiting)
            execute(
                connection,
                "UPDATE runtime_sessions SET state='WAITING_APPROVAL',approval_wait_started_at=? WHERE task_id=? AND approval_mode='agent'",
                (time.time(), task.task_id.hex),
            )
        return waiting, approval

    def get_pending_approval(self, task_id: UUID) -> PendingApproval | None:
        record = self.get_approval(task_id)
        if record is None or record.decision is not None:
            return None
        return record.request

    def get_approval(self, task_id: UUID) -> ApprovalRecord | None:
        if not database_exists(self._database):
            return None
        with self._engine.connect() as connection:
            schema = inspect(connection)
            if not schema.has_table(_PENDING_APPROVALS.name):
                return None
            return self._read_approval(
                connection,
                task_id,
                with_decision=schema.has_table(_APPROVAL_DECISIONS.name),
                with_policy=any(
                    column["name"] == "policy_version"
                    for column in schema.get_columns(_PENDING_APPROVALS.name)
                ),
            )

    def decide_approval(
        self,
        approval_id: UUID,
        outcome: ApprovalOutcome,
        arguments_sha256: str,
        *,
        actor: ApprovalActor,
    ) -> ApprovalDecision:
        if not actor.can_approve or not actor.subject.strip():
            raise PermissionError("The caller does not have approval permission")

        with self._engine.begin() as connection:
            task_id = connection.execute(
                select(_PENDING_APPROVALS.c.task_id).where(
                    _PENDING_APPROVALS.c.approval_id == approval_id
                )
            ).scalar_one_or_none()
            if task_id is None:
                raise ApprovalConflictError("Approval request not found")
            lock_task(connection, task_id)
            row = connection.execute(
                select(_PENDING_APPROVALS, _TASKS.c.status, _TASKS.c.revision)
                .join(_TASKS)
                .where(_PENDING_APPROVALS.c.approval_id == approval_id)
            ).one_or_none()
            if row is None:
                raise ApprovalConflictError("Approval request not found")
            if self._read_decision(connection, approval_id) is not None:
                raise ApprovalConflictError("Approval request has already been decided")
            if row.status is not TaskStatus.WAITING_APPROVAL:
                raise ApprovalConflictError("Task is no longer waiting for approval")
            if (
                self._read_approval(connection, task_id).request.approval_id
                != approval_id
            ):
                raise ApprovalConflictError("Approval request is no longer current")

            intent = ApprovalIntent(
                row.tool_name, row.tool_version, row.canonical_arguments
            )
            if arguments_sha256 != intent.arguments_sha256:
                raise ApprovalConflictError(
                    "Approval request arguments do not match the digest"
                )

            decision = ApprovalDecision(
                outcome,
                actor.subject,
                arguments_sha256,
                datetime.now(UTC).isoformat(timespec="seconds"),
            )
            connection.execute(
                _APPROVAL_DECISIONS.insert().values(
                    approval_id=approval_id,
                    outcome=decision.outcome,
                    decided_by=decision.decided_by,
                    arguments_sha256=decision.arguments_sha256,
                    decided_at=decision.decided_at,
                )
            )
            if outcome is ApprovalOutcome.REJECTED:
                waiting = Task(row.task_id, row.status, row.revision)
                self._update_task(
                    connection, waiting, waiting.transition(TaskStatus.FAILED)
                )
            else:
                wake_approval(connection, task_id)
        return decision

    def resume(
        self,
        task_id: UUID,
        workspace: TicketStore,
        *,
        after_business_commit: Callable[[], None] | None = None,
        checkpoint_owner: tuple[str, int] | None = None,
    ) -> Task:
        identity = self._claim_execution(
            task_id, workspace, checkpoint_owner=checkpoint_owner
        )
        with self._engine.begin() as connection:
            # Cancellation and dispatch share this lock. The intent is already durable.
            lock_task(connection, task_id)
            self._check_checkpoint_owner(connection, task_id, checkpoint_owner)
            task = self._require_task(connection, task_id)
            execution = self._read_execution(connection, task_id)
            if execution.status != "INTENT":
                return task
            if task.status is not TaskStatus.RUNNING:
                raise ExecutionConflictError(
                    "Execution intent cannot run in the current task state"
                )

            try:
                record = self._require_approved(connection, task_id)
                intent = record.request.intent
                if (intent.tool_name, intent.tool_version, intent.arguments_sha256) != (
                    execution.tool_name,
                    execution.tool_version,
                    execution.arguments_sha256,
                ):
                    raise EvidenceUnavailable(
                        "Approval no longer matches the recorded execution intent"
                    )
                args = UpdateTicketStatusArgs.model_validate_json(
                    intent.canonical_arguments
                )
                key = execution.operation_id.hex
                receipt = workspace.receipt(execution.workspace_id, key)
                detail = "reconciled_business_receipt"
                if receipt is None:
                    try:
                        self._check_dispatch(record)
                        if execution.policy_version != TICKET_POLICY_VERSION:
                            raise ExecutionConflictError("Execution policy has changed")
                    except ExecutionConflictError as exc:
                        return self._finish_execution(
                            connection,
                            task,
                            execution.operation_id,
                            "FAILED",
                            str(exc),
                            identity=identity,
                        )
                    receipt = workspace.apply(
                        execution.workspace_id,
                        key,
                        execution.arguments_sha256,
                        args.ticket_id,
                        args.status,
                    )
                    detail = "verified_business_write"
                    if after_business_commit is not None:
                        after_business_commit()

                if (receipt.digest, receipt.ticket_id, receipt.status) != (
                    execution.arguments_sha256,
                    args.ticket_id,
                    args.status,
                ):
                    raise EvidenceUnavailable(
                        "Business receipt does not match the approved request"
                    )
            except (EvidenceUnavailable, ExecutionConflictError, ValueError) as exc:
                return self._finish_execution(
                    connection,
                    task,
                    execution.operation_id,
                    "UNKNOWN",
                    str(exc),
                    identity=identity,
                )

            return self._finish_execution(
                connection,
                task,
                execution.operation_id,
                "SUCCEEDED",
                detail,
                ticket_revision=receipt.revision,
                identity=identity,
            )

    def cancel(self, task_id: UUID, workspace: TicketStore) -> Task:
        with self._engine.begin() as connection:
            lock_task(connection, task_id)
            task = self._require_task(connection, task_id)
            if task.status is TaskStatus.CANCELLED:
                return task
            service_request = connection.execute(
                select(_SERVICE_TASKS.c.request_json).where(
                    _SERVICE_TASKS.c.task_id == task_id
                )
            ).scalar_one_or_none()
            agent_session = (
                execute(
                    connection,
                    "SELECT approval_mode FROM runtime_sessions WHERE task_id=?",
                    (task_id.hex,),
                ).scalar_one_or_none()
                == "agent"
            )
            if (service_request is not None or agent_session) and (
                task.status is TaskStatus.CREATED
                or (
                    task.status is TaskStatus.RUNNING
                    and (
                        self._read_execution(connection, task_id) is None
                        or self._read_execution(connection, task_id).status
                        == "SUCCEEDED"
                    )
                )
            ):
                cancelled = task.transition(TaskStatus.CANCELLED)
                self._update_task(connection, task, cancelled)
                return cancelled
            if task.status is TaskStatus.WAITING_APPROVAL:
                cancelled = task.transition(TaskStatus.CANCELLED)
                self._update_task(connection, task, cancelled)
                return cancelled

            execution = self._read_execution(connection, task_id)
            if (
                task.status is not TaskStatus.RUNNING
                or execution is None
                or execution.status != "INTENT"
            ):
                raise ExecutionConflictError(
                    "Task cannot be cancelled in its current state"
                )
            try:
                receipt = workspace.receipt(
                    execution.workspace_id, execution.operation_id.hex
                )
            except EvidenceUnavailable as exc:
                return self._finish_execution(
                    connection, task, execution.operation_id, "UNKNOWN", str(exc)
                )
            if receipt is not None:
                raise ExecutionConflictError(
                    "Business write already committed; resume to reconcile the receipt"
                )
            return self._finish_execution(
                connection,
                task,
                execution.operation_id,
                "CANCELLED",
                "cancelled_before_dispatch",
            )

    def get_execution(self, task_id: UUID) -> dict[str, object] | None:
        if not database_exists(self._database):
            return None
        with self._engine.connect() as connection:
            if not inspect(connection).has_table(_EXECUTIONS.name):
                return None
            row = self._read_execution(connection, task_id)
        if row is None:
            return None
        return {
            **row._mapping,
            "operation_id": str(row.operation_id),
            "task_id": str(task_id),
        }

    def read_ticket(self, workspace: TicketStore) -> tuple[str, int]:
        workspace_id = None
        if database_exists(self._database):
            with self._engine.connect() as connection:
                if inspect(connection).has_table(_EXECUTIONS.name):
                    workspace_id = self._read_workspace_id(connection)
        return workspace.ticket(workspace_id)

    def _claim_execution(
        self, task_id: UUID, workspace: TicketStore, *, checkpoint_owner=None
    ) -> CallIdentity | None:
        with self._engine.begin() as connection:
            lock_task(connection, task_id)
            task = self._require_task(connection, task_id)
            execution = self._read_execution(connection, task_id)
            if execution is not None:
                self._check_checkpoint_owner(connection, task_id, checkpoint_owner)
                if execution.status != "INTENT":
                    return None
                record = self._require_approved(connection, task_id)
                call_id = approval_call(
                    connection,
                    task_id,
                    record.request.approval_id,
                    record.request.intent,
                )
                return new_attempt(
                    connection,
                    task_id,
                    ensure_run(connection, task_id, "business"),
                    call_id,
                    "business",
                )
            if task.status is not TaskStatus.WAITING_APPROVAL:
                raise ExecutionConflictError(
                    "Task is not waiting for an approved execution"
                )
            self._check_checkpoint_owner(connection, task_id, checkpoint_owner)
            record = self._require_approved(connection, task_id)
            self._check_dispatch(record)
            session = execute(
                connection,
                "SELECT approval_mode,approval_wait_started_at,deadline_at FROM runtime_sessions WHERE task_id=?",
                (task_id.hex,),
            ).fetchone()
            if session and session[0] == "agent" and session[1] is not None:
                execute(
                    connection,
                    "UPDATE runtime_sessions SET deadline_at=?,approval_wait_started_at=NULL WHERE task_id=?",
                    (session[2] + max(0, time.time() - session[1]), task_id.hex),
                )
            operation_id = record.request.approval_id
            intent = record.request.intent
            workspace_id = self._read_workspace_id(connection)
            if workspace_id is None:
                workspace_id = workspace.initialize()
            else:
                workspace.receipt(workspace_id, operation_id.hex)
            self._update_task(connection, task, task.transition(TaskStatus.RUNNING))
            call_id = approval_call(connection, task_id, operation_id, intent)
            identity = new_attempt(
                connection,
                task_id,
                ensure_run(connection, task_id, "business"),
                call_id,
                "business",
            )
            connection.execute(
                _EXECUTIONS.insert().values(
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                    policy_version=TICKET_POLICY_VERSION,
                    tool_name=intent.tool_name,
                    tool_version=intent.tool_version,
                    arguments_sha256=intent.arguments_sha256,
                    status="INTENT",
                    claimed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            )
            insert_event(
                connection,
                task_id,
                "execution.intent",
                {
                    "call_id": str(identity.call_id),
                    "attempt_id": str(identity.attempt_id),
                    "operation_id": str(operation_id),
                    "tool_name": intent.tool_name,
                    "arguments_sha256": intent.arguments_sha256,
                    "policy_version": TICKET_POLICY_VERSION,
                },
                run_id=identity.run_id,
            )
            return identity

    @staticmethod
    def _read_workspace_id(connection: Connection) -> str | None:
        return connection.execute(
            select(_EXECUTIONS.c.workspace_id).limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def _read_task(connection: Connection, task_id: UUID) -> Task | None:
        row = connection.execute(
            select(_TASKS).where(_TASKS.c.task_id == task_id)
        ).one_or_none()
        return Task(row.task_id, row.status, row.revision) if row is not None else None

    def _require_task(self, connection: Connection, task_id: UUID) -> Task:
        task = self._read_task(connection, task_id)
        if task is None:
            raise ExecutionConflictError("Task not found")
        return task

    def _read_approval(
        self,
        connection: Connection,
        task_id: UUID,
        *,
        with_decision: bool = True,
        with_policy: bool = True,
    ) -> ApprovalRecord | None:
        request_fields = _PENDING_APPROVALS.c
        policy_version = (
            request_fields.policy_version
            if with_policy
            else literal("mock-ticket-v1").label("policy_version")
        )
        query = select(
            request_fields.approval_id,
            request_fields.task_id,
            request_fields.tool_name,
            request_fields.tool_version,
            request_fields.canonical_arguments,
            policy_version,
        ).where(request_fields.task_id == task_id)
        if "sequence" in {
            c["name"] for c in inspect(connection).get_columns("pending_approvals")
        }:
            query = query.order_by(request_fields.sequence.desc()).limit(1)
        row = connection.execute(query).one_or_none()
        if row is None:
            return None
        intent = ApprovalIntent(
            row.tool_name, row.tool_version, row.canonical_arguments
        )
        decision = (
            self._read_decision(connection, row.approval_id) if with_decision else None
        )
        return ApprovalRecord(
            PendingApproval(row.approval_id, row.task_id, intent, row.policy_version),
            decision,
        )

    def _require_approved(
        self, connection: Connection, task_id: UUID
    ) -> ApprovalRecord:
        record = self._read_approval(connection, task_id)
        if (
            record is None
            or record.decision is None
            or record.decision.outcome is not ApprovalOutcome.APPROVED
        ):
            raise ExecutionConflictError("An approved request is required")
        if record.request.intent.arguments_sha256 != record.decision.arguments_sha256:
            raise ExecutionConflictError(
                "Stored arguments do not match the approved digest"
            )
        return record

    @staticmethod
    def _check_dispatch(record: ApprovalRecord) -> None:
        if record.request.policy_version != TICKET_POLICY_VERSION:
            raise ExecutionConflictError("Approval policy version has changed")
        try:
            decided_at = datetime.fromisoformat(record.decision.decided_at)
            age = datetime.now(UTC) - decided_at
            if not timedelta(0) <= age < _APPROVAL_TTL:
                raise ExecutionConflictError(
                    "Approval is outside its 24-hour execution window"
                )
            verify_update_intent(record.request.intent)
        except (TypeError, ValueError) as exc:
            raise ExecutionConflictError(
                "Approval no longer matches the tool policy"
            ) from exc

    @staticmethod
    def _read_execution(connection: Connection, task_id: UUID):
        approvals = select(_PENDING_APPROVALS.c.approval_id).where(
            _PENDING_APPROVALS.c.task_id == task_id
        )
        if "sequence" in {
            c["name"] for c in inspect(connection).get_columns("pending_approvals")
        }:
            approvals = approvals.order_by(_PENDING_APPROVALS.c.sequence.desc())
        return connection.execute(
            select(_EXECUTIONS)
            .join(
                _PENDING_APPROVALS,
                _EXECUTIONS.c.operation_id == _PENDING_APPROVALS.c.approval_id,
            )
            .where(
                _EXECUTIONS.c.operation_id
                == approvals.limit(1).correlate(None).scalar_subquery()
            )
        ).one_or_none()

    def _finish_execution(
        self,
        connection: Connection,
        task: Task,
        operation_id: UUID,
        status: str,
        detail: str,
        *,
        ticket_revision: int | None = None,
        identity: CallIdentity | None = None,
    ) -> Task:
        pending = (
            execute(
                connection,
                "SELECT a.attempt_id FROM call_attempts a JOIN logical_calls c ON c.call_id=a.call_id WHERE c.operation_id=? AND a.task_id=? AND a.status='INTENT' AND a.kind='business' ORDER BY a.attempt_index",
                (operation_id.hex, task.task_id.hex),
            )
            .scalars()
            .all()
        )
        if identity is None and pending:
            identity = identity_for_attempt(connection, UUID(pending[-1]))
        if identity is not None:
            code = (
                "business_outcome_unknown"
                if status == "UNKNOWN"
                else (
                    detail
                    if detail
                    in {
                        "verified_business_write",
                        "reconciled_business_receipt",
                        "cancelled_before_dispatch",
                    }
                    else "business_dispatch_rejected"
                )
            )
            finish_attempt(
                connection,
                identity,
                status,
                code,
                json.dumps(
                    {
                        "operation_id": str(operation_id),
                        "status": status,
                        "ticket_revision": ticket_revision,
                    }
                ),
                artifact_kind="business_receipt",
            )
            for identifier in pending:
                if identifier == identity.attempt_id.hex:
                    continue
                other = identity_for_attempt(connection, UUID(identifier))
                other_status = (
                    (
                        "RECONCILED"
                        if detail == "reconciled_business_receipt"
                        else "REUSED"
                    )
                    if status == "SUCCEEDED"
                    else status
                )
                finish_attempt(connection, other, other_status, code, settle_call=False)
        agent = (
            execute(
                connection,
                "SELECT approval_mode FROM runtime_sessions WHERE task_id=?",
                (task.task_id.hex,),
            ).scalar_one_or_none()
            == "agent"
        )
        if status == "SUCCEEDED" and not agent:
            verifying = task.transition(TaskStatus.VERIFYING)
            self._update_task(connection, task, verifying)
            task = verifying
        if status == "SUCCEEDED" and agent:
            finished = task
            close_runs(connection, task.task_id, "SUCCEEDED")
            ensure_run(connection, task.task_id, "agent")
        else:
            target = (
                TaskStatus.NEEDS_REVIEW if status == "UNKNOWN" else TaskStatus[status]
            )
            finished = task.transition(target)
            self._update_task(connection, task, finished)
        connection.execute(
            _EXECUTIONS.update()
            .where(_EXECUTIONS.c.operation_id == operation_id)
            .values(
                status=status,
                detail=detail,
                ticket_revision=ticket_revision,
                finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        )
        insert_event(
            connection,
            task.task_id,
            "execution.result",
            {
                "operation_id": str(operation_id),
                "status": status,
                **(
                    {
                        "call_id": str(identity.call_id),
                        "attempt_id": str(identity.attempt_id),
                    }
                    if identity
                    else {}
                ),
            },
            run_id=identity.run_id if identity else None,
        )
        return finished

    @staticmethod
    def _check_checkpoint_owner(connection, task_id, owner):
        require_claim(connection, task_id)
        row = execute(
            connection,
            "SELECT owner_token,generation FROM runtime_sessions WHERE task_id=?",
            (task_id.hex,),
        ).fetchone()
        if row and row[0] is not None and (owner is None or tuple(row) != owner):
            raise ExecutionConflictError("An active checkpoint runner owns this task")

    @staticmethod
    def _read_decision(
        connection: Connection, approval_id: UUID
    ) -> ApprovalDecision | None:
        row = connection.execute(
            select(_APPROVAL_DECISIONS).where(
                _APPROVAL_DECISIONS.c.approval_id == approval_id
            )
        ).one_or_none()
        if row is None:
            return None
        return ApprovalDecision(
            row.outcome, row.decided_by, row.arguments_sha256, row.decided_at
        )

    @staticmethod
    def _update_task(connection: Connection, task: Task, updated: Task) -> None:
        approval_stop = (
            task.status is TaskStatus.WAITING_APPROVAL
            and updated.status is TaskStatus.FAILED
        )
        if updated.status is not TaskStatus.CANCELLED and not approval_stop:
            try:
                require_claim(connection, task.task_id)
            except QueueClaimError as exc:
                raise TaskConflictError(str(exc)) from None
        result = connection.execute(
            _TASKS.update()
            .where(
                _TASKS.c.task_id == task.task_id,
                _TASKS.c.status == task.status,
                _TASKS.c.revision == task.revision,
            )
            .values(status=updated.status, revision=updated.revision)
        )
        if result.rowcount != 1:
            raise TaskConflictError(f"Task {task.task_id} changed or no longer exists")
        if updated.status is TaskStatus.CANCELLED:
            cancel_queued(connection, task.task_id)
        elif updated.status is TaskStatus.WAITING_APPROVAL:
            execute(
                connection,
                "UPDATE task_queue SET state='WAITING' WHERE task_id=? AND state='CLAIMED'",
                (task.task_id.hex,),
            )
        elif approval_stop:
            execute(
                connection,
                "UPDATE task_queue SET state='FINISHED',finished_at=? WHERE task_id=? AND state IN ('WAITING','READY')",
                (now(), task.task_id.hex),
            )
        elif updated.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.NEEDS_REVIEW,
        }:
            finish_claim(connection, task.task_id, updated.status.value)
        if updated.status is TaskStatus.RUNNING:
            ensure_run(
                connection,
                task.task_id,
                "business" if task.status is TaskStatus.WAITING_APPROVAL else "agent",
            )
        insert_event(
            connection,
            task.task_id,
            "task.transition",
            {
                "from_status": task.status.value,
                "status": updated.status.value,
                "revision": updated.revision,
            },
        )
        if updated.status not in {
            TaskStatus.CREATED,
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
        }:
            close_runs(connection, task.task_id, updated.status.value)
            if updated.status is not TaskStatus.WAITING_APPROVAL:
                execute(
                    connection,
                    "UPDATE runtime_sessions SET state=? WHERE task_id=? AND owner_token IS NULL",
                    (
                        "NEEDS_REVIEW"
                        if updated.status is TaskStatus.NEEDS_REVIEW
                        else "FINISHED",
                        task.task_id.hex,
                    ),
                )
        if task.status is TaskStatus.WAITING_APPROVAL and updated.status in {
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            execute(
                connection,
                "UPDATE logical_calls SET status=?,code=?,finished_at=? WHERE task_id=? AND status='WAITING_APPROVAL'",
                (
                    updated.status.value,
                    "approval_rejected"
                    if updated.status is TaskStatus.FAILED
                    else "task_cancelled",
                    now(),
                    task.task_id.hex,
                ),
            )
