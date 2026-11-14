"""A durable invocation journal shared by all tool execution targets."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from .contracts import CURRENT_CALL, InvocationConflictError
from .database import (
    database_exists,
    engine_for,
    execute,
    lock_task,
    normalize_database,
)
from .failures import ToolExecutionError
from .invocations import (
    bind_policy,
    ensure_run,
    finish_attempt,
    identity_for_attempt,
    new_attempt,
    new_call,
    tool_status,
)
from .scheduling import QueueClaimError, require_claim
from .trace import insert_event


class AuditError(ToolExecutionError):
    def __init__(self, message, *, outcome_unknown=False):
        self.outcome_unknown = outcome_unknown
        self.code = (
            "audit_result_error" if outcome_unknown else "audit_before_dispatch_error"
        )
        RuntimeError.__init__(self, message)


class AuditLog:
    def __init__(self, database: Path, task_id: UUID):
        self.database = normalize_database(database)
        self._engine = engine_for(self.database)
        self.task_id = task_id

    def begin(
        self,
        *,
        tool: str,
        version: int | None,
        policy: str,
        digest: str | None,
        resources: tuple[str, ...],
        decision: str,
        code: str,
        verification: bool = False,
    ) -> UUID:
        try:
            if not database_exists(self.database):
                raise AuditError("Audit database is not initialized")
            with self._engine.begin() as connection:
                lock_task(connection, self.task_id)
                row = execute(
                    connection,
                    "SELECT status FROM tasks WHERE task_id=?",
                    (self.task_id.hex,),
                ).fetchone()
                if row != (("VERIFYING",) if verification else ("RUNNING",)):
                    raise AuditError("Task is not running; tool dispatch is forbidden")
                require_claim(connection, self.task_id)
                identity = CURRENT_CALL.get()
                if identity is not None and identity.task_id != self.task_id:
                    raise InvocationConflictError("Invocation task mismatch")
                if identity is None:
                    run_id = ensure_run(connection, self.task_id)
                    kind = "verification" if verification else "tool"
                    call_id = new_call(
                        connection, self.task_id, run_id, tool, kind, "gateway"
                    )
                    identity = new_attempt(
                        connection, self.task_id, run_id, call_id, kind
                    )
                bind_policy(
                    connection, identity, tool, version, policy, digest, decision
                )
                if execute(
                    connection,
                    "SELECT 1 FROM gateway_events WHERE attempt_id=? AND phase='POLICY'",
                    (identity.attempt_id.hex,),
                ).first():
                    raise InvocationConflictError("An attempt can dispatch only once")
                call_id = identity.call_id
                fields = (tool[:128], version, policy, digest, json.dumps(resources))
                self._insert(
                    connection,
                    call_id,
                    "POLICY",
                    fields,
                    decision,
                    code,
                    0,
                    identity=identity,
                )
                if decision == "ALLOW":
                    self._insert(
                        connection,
                        call_id,
                        "INTENT",
                        fields,
                        "READY",
                        "authorized_dispatch",
                        0,
                        identity=identity,
                    )
        except (SQLAlchemyError, InvocationConflictError, QueueClaimError) as exc:
            raise AuditError(
                "Could not persist the tool decision before dispatch"
            ) from exc
        return call_id

    def finish(
        self, call_id: UUID, status: str, code: str, elapsed_ms: int, *, value=None
    ):
        try:
            with self._engine.begin() as connection:
                lock_task(connection, self.task_id)
                require_claim(connection, self.task_id)
                row = execute(
                    connection,
                    "SELECT tool_name, tool_version, policy_version, arguments_sha256, resources,attempt_id FROM gateway_events WHERE call_id=? AND task_id=? ORDER BY event_id LIMIT 1",
                    (call_id.hex, self.task_id.hex),
                ).fetchone()
                if row is None:
                    raise AuditError(
                        "Missing persisted policy decision", outcome_unknown=True
                    )
                if row[5] is None:
                    raise InvocationConflictError(
                        "Legacy event has no dispatch identity"
                    )
                identity = identity_for_attempt(connection, UUID(row[5]))
                finish_attempt(
                    connection, identity, tool_status(status, code), code, value
                )
                if execute(
                    connection,
                    "SELECT 1 FROM gateway_events WHERE attempt_id=? AND phase='RESULT'",
                    (identity.attempt_id.hex,),
                ).first():
                    return
                self._insert(
                    connection,
                    call_id,
                    "RESULT",
                    row[:5],
                    status,
                    code,
                    elapsed_ms,
                    identity=identity,
                )
        except (SQLAlchemyError, InvocationConflictError, QueueClaimError) as exc:
            raise AuditError(
                "Could not persist the tool result", outcome_unknown=True
            ) from exc

    def identity(self, call_id):
        with self._engine.connect() as connection:
            identifier = execute(
                connection,
                "SELECT attempt_id FROM gateway_events WHERE call_id=? AND task_id=? AND phase='POLICY' ORDER BY event_id LIMIT 1",
                (call_id.hex, self.task_id.hex),
            ).scalar_one()
            return identity_for_attempt(connection, UUID(identifier))

    def _insert(
        self, connection, call_id, phase, fields, status, code, elapsed, *, identity
    ):
        execute(
            connection,
            "INSERT INTO gateway_events (schema_version, task_id, call_id, phase, tool_name, tool_version, policy_version, arguments_sha256, resources, status, code, elapsed_ms, recorded_at,run_id,attempt_id) VALUES (2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.task_id.hex,
                call_id.hex,
                phase,
                *fields,
                status,
                code,
                elapsed,
                datetime.now(UTC).isoformat(timespec="seconds"),
                identity.run_id.hex,
                identity.attempt_id.hex,
            ),
        )
        insert_event(
            connection,
            self.task_id,
            "tool." + phase.lower(),
            {
                "call_id": str(call_id),
                "attempt_id": str(identity.attempt_id),
                "tool_name": fields[0],
                "tool_version": fields[1],
                "policy_version": fields[2],
                "arguments_sha256": fields[3],
                "status": status,
                "code": code,
                "elapsed_ms": elapsed,
            },
            run_id=identity.run_id,
        )

    def read(self) -> list[dict]:
        if not database_exists(self.database):
            return []
        with self._engine.connect() as connection:
            if not inspect(connection).has_table("gateway_events"):
                return []
            rows = (
                execute(
                    connection,
                    "SELECT * FROM gateway_events WHERE task_id=? ORDER BY event_id",
                    (self.task_id.hex,),
                )
                .mappings()
                .all()
            )
        return [
            {
                **dict(row),
                "task_id": str(self.task_id),
                "call_id": str(UUID(row["call_id"])),
                "run_id": str(UUID(row["run_id"])) if row["run_id"] else None,
                "attempt_id": str(UUID(row["attempt_id"]))
                if row["attempt_id"]
                else None,
                "resources": json.loads(row["resources"]),
            }
            for row in rows
        ]
