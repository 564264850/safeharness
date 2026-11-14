"""Safe-point snapshots. Recovery never replays an unconfirmed tool invocation."""

import hashlib
import json
import time
from uuid import UUID, uuid4

from .approval import ApprovalIntent
from .context import encoded
from .database import execute, lock_task
from .execution import ToolRequest, ToolResult
from .invocations import active_run, digest, now
from .scheduling import QueueClaimError, require_claim
from .task import Task, TaskStatus
from .trace import insert_event

MAX_CHECKPOINT_BYTES = 2097152


class CheckpointError(RuntimeError):
    def __init__(self, code, *, outcome_unknown=False):
        self.code, self.outcome_unknown = code, outcome_unknown
        super().__init__(code)


class CheckpointConflict(CheckpointError):
    pass


def result_state(result):
    value = {
        "name": result.name,
        "status": result.status,
        "code": result.code,
        "value": result.value,
        "approval": None
        if result.approval is None
        else {
            "tool_name": result.approval.tool_name,
            "tool_version": result.approval.tool_version,
            "canonical_arguments": result.approval.canonical_arguments,
        },
    }
    for key in ("call_id", "attempt_id", "run_id"):
        identifier = getattr(result, key)
        value[key] = str(identifier) if identifier else None
    return value


def restore_result(value):
    value = dict(value)
    if value["approval"]:
        value["approval"] = ApprovalIntent(**value["approval"])
    for key in ("call_id", "attempt_id", "run_id"):
        value[key] = UUID(value[key]) if value[key] else None
    return ToolResult(**value)


class CheckpointSession:
    def __init__(
        self,
        store,
        task_id,
        configuration,
        *,
        model,
        workspace=None,
        stateless_model=False,
        timeout_seconds=120,
        approval_mode="agent",
    ):
        if not stateless_model and not all(
            callable(getattr(model, name, None)) for name in ("snapshot", "restore")
        ):
            raise ValueError(
                "Checkpoint model needs snapshot/restore or an explicit stateless contract"
            )
        if not 0 < timeout_seconds <= 86400:
            raise ValueError("Invalid session time budget")
        self.store, self.task_id, self.model, self.workspace = (
            store,
            task_id,
            model,
            workspace,
        )
        self.stateless_model, self.timeout_seconds = stateless_model, timeout_seconds
        if approval_mode not in {"agent", "legacy"}:
            raise ValueError("Invalid approval mode")
        self.approval_mode = approval_mode
        self.configuration = {
            "version": 1,
            "application": configuration,
            "model": getattr(model, "identity", None),
            "stateless_model": stateless_model,
            "workspace": getattr(workspace, "identity", None),
        }
        self.config_sha = digest(self.configuration)
        if approval_mode == "agent":
            self.configuration["approval_mode"] = approval_mode
            self.config_sha = digest(self.configuration)
        self.token, self.generation, self.deadline_at = None, None, None
        self.state, self.steps, self.pending_action = None, 0, None

    def _session(self, connection):
        return (
            execute(
                connection,
                "SELECT * FROM runtime_sessions WHERE task_id=?",
                (self.task_id.hex,),
            )
            .mappings()
            .one_or_none()
        )

    def _owned(self, connection):
        try:
            require_claim(connection, self.task_id)
        except QueueClaimError as exc:
            raise CheckpointConflict(str(exc)) from None
        session = self._session(connection)
        if (
            session is None
            or session["owner_token"] != self.token
            or session["generation"] != self.generation
        ):
            raise CheckpointConflict("checkpoint_runner_fenced")
        return session

    def begin(self, task, max_steps):
        with self.store._engine.begin() as connection:
            lock_task(connection, self.task_id)
            if (
                self._session(connection) is not None
                or self.store._require_task(connection, self.task_id) != task
            ):
                raise CheckpointConflict("checkpoint_session_exists")
            try:
                claim = require_claim(connection, self.task_id)
            except QueueClaimError as exc:
                raise CheckpointConflict(str(exc)) from None
            self.token, self.generation = (
                (claim.token, claim.generation) if claim else (uuid4().hex, 1)
            )
            self.deadline_at = time.time() + self.timeout_seconds
            if claim:
                deadline = execute(
                    connection,
                    "SELECT execution_deadline_at FROM task_queue WHERE task_id=?",
                    (self.task_id.hex,),
                ).scalar_one()
                self.deadline_at = min(self.deadline_at, deadline)
            execute(
                connection,
                "INSERT INTO runtime_sessions (task_id,schema_version,config_sha256,generation,owner_token,state,checkpoint_id,deadline_at,max_steps,created_at,approval_mode) VALUES (?,?,?,?,?,?,NULL,?,?,?,?)",
                (
                    self.task_id.hex,
                    1,
                    self.config_sha,
                    self.generation,
                    self.token,
                    "RUNNING",
                    self.deadline_at,
                    max_steps,
                    now(),
                    self.approval_mode,
                ),
            )
        self.save(task, "initial", (), 0)

    def acquire(
        self,
        *,
        max_steps,
        recover_abandoned=False,
        expected_generation=None,
        allow_approval=False,
    ):
        with self.store._engine.begin() as connection:
            lock_task(connection, self.task_id)
            try:
                queue_claim = require_claim(connection, self.task_id)
            except QueueClaimError as exc:
                raise CheckpointConflict(str(exc)) from None
            task = self.store._require_task(connection, self.task_id)
            session = self._session(connection)
            if (
                queue_claim
                and queue_claim.recovery_mode in {"resume", "approval"}
                and (
                    session is None
                    or session["checkpoint_id"] != queue_claim.checkpoint_id
                )
            ):
                raise CheckpointConflict("recovery_checkpoint_changed")
            if (
                task.status
                not in (
                    {
                        TaskStatus.RUNNING,
                        TaskStatus.VERIFYING,
                        TaskStatus.WAITING_APPROVAL,
                    }
                    if allow_approval
                    else {TaskStatus.RUNNING, TaskStatus.VERIFYING}
                )
                or session is None
            ):
                raise CheckpointConflict("checkpoint_task_not_resumable")
            if (
                session["schema_version"] != 1
                or session["config_sha256"] != self.config_sha
                or session["max_steps"] != max_steps
            ):
                raise CheckpointConflict("checkpoint_configuration_changed")
            if (
                expected_generation is not None
                and session["generation"] != expected_generation
            ):
                raise CheckpointConflict("checkpoint_generation_changed")
            if session["owner_token"] is not None and (
                not recover_abandoned or expected_generation is None
            ):
                raise CheckpointConflict("checkpoint_runner_active")
            approval_wait = (
                allow_approval
                and session["approval_mode"] == "agent"
                and session["state"] == "WAITING_APPROVAL"
            )
            if (
                session["state"] != "PAUSED"
                and not approval_wait
                and not recover_abandoned
            ):
                raise CheckpointConflict("checkpoint_not_paused")
            checkpoint = (
                execute(
                    connection,
                    "SELECT * FROM runtime_checkpoints WHERE checkpoint_id=? AND task_id=?",
                    (session["checkpoint_id"], self.task_id.hex),
                )
                .mappings()
                .one_or_none()
            )
            if (
                checkpoint is None
                or checkpoint["byte_count"] != len(bytes(checkpoint["content"]))
                or checkpoint["byte_count"] > MAX_CHECKPOINT_BYTES
                or checkpoint["sha256"]
                != hashlib.sha256(bytes(checkpoint["content"])).hexdigest()
            ):
                raise CheckpointConflict("checkpoint_integrity_error")
            try:
                payload = json.loads(bytes(checkpoint["content"]))
            except (TypeError, ValueError):
                raise CheckpointConflict("checkpoint_schema_error") from None
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or type(payload.get("steps")) is not int
                or payload["steps"] != checkpoint["step_count"]
                or not 0 <= payload["steps"] <= max_steps
                or not isinstance(payload.get("results"), list)
                or len(payload["results"]) > payload["steps"]
                or not {"model", "workspace", "action"} <= payload.keys()
            ):
                raise CheckpointConflict("checkpoint_schema_error")
            if task.status is TaskStatus.VERIFYING and checkpoint["phase"] != "final":
                raise CheckpointConflict("checkpoint_verification_not_ready")
            pending = payload["results"][-1] if payload["results"] else None
            if pending and pending.get("status") == "approval_required":
                if (
                    not allow_approval
                    or session["approval_mode"] != "agent"
                    or checkpoint["phase"] != "after_tool"
                ):
                    raise CheckpointConflict("approval_continuation_required")
                try:
                    record = self.store._require_approved(connection, self.task_id)
                except RuntimeError:
                    raise CheckpointConflict("approval_not_approved") from None
                binding = execute(
                    connection,
                    "SELECT call_id FROM approval_calls WHERE approval_id=? AND task_id=?",
                    (record.request.approval_id.hex, self.task_id.hex),
                ).scalar_one_or_none()
                expected = {
                    "tool_name": record.request.intent.tool_name,
                    "tool_version": record.request.intent.tool_version,
                    "canonical_arguments": record.request.intent.canonical_arguments,
                }
                if (
                    binding is None
                    or pending.get("approval") != expected
                    or pending.get("call_id") != str(UUID(binding))
                ):
                    raise CheckpointConflict("approval_checkpoint_mismatch")
            if queue_claim:
                if queue_claim.generation <= session["generation"]:
                    raise CheckpointConflict("queue_generation_not_advanced")
                self.token, self.generation = queue_claim.token, queue_claim.generation
            else:
                self.token, self.generation = uuid4().hex, session["generation"] + 1
            self.deadline_at = session["deadline_at"]
            if approval_wait and session["approval_wait_started_at"] is not None:
                self.deadline_at += max(
                    0, time.time() - session["approval_wait_started_at"]
                )
            if queue_claim:
                queued_deadline = execute(
                    connection,
                    "SELECT execution_deadline_at FROM task_queue WHERE task_id=?",
                    (self.task_id.hex,),
                ).scalar_one()
                self.deadline_at = min(self.deadline_at, queued_deadline)
            execute(
                connection,
                "UPDATE runtime_sessions SET owner_token=?,generation=?,state='RUNNING',deadline_at=?,approval_wait_started_at=NULL WHERE task_id=?",
                (self.token, self.generation, self.deadline_at, self.task_id.hex),
            )
            # Fence stale Task snapshots as well as checkpoint owners.
            execute(
                connection,
                "UPDATE tasks SET revision=revision+1 WHERE task_id=?",
                (self.task_id.hex,),
            )
            task = Task(task.task_id, task.status, task.revision + 1)
            insert_event(
                connection,
                self.task_id,
                "checkpoint.restored",
                {
                    "checkpoint_id": str(UUID(checkpoint["checkpoint_id"])),
                    "generation": self.generation,
                    "step_count": payload["steps"],
                    "phase": checkpoint["phase"],
                    "revision": task.revision,
                },
            )
        self.state, self.steps = payload, payload["steps"]
        self.phase = checkpoint["phase"]
        return task

    def restore(self):
        if self.phase == "before_tool":
            raise CheckpointError(
                "checkpoint_tool_outcome_unconfirmed", outcome_unknown=True
            )
        if time.time() >= self.deadline_at:
            raise CheckpointError("checkpoint_deadline_exceeded")
        if not self.stateless_model:
            self.model.restore(self.state["model"])
        if self.workspace is not None:
            self.workspace.restore(self.state["workspace"])
            self.workspace.deadline = min(
                self.workspace.deadline,
                time.monotonic() + self.deadline_at - time.time(),
            )
        if hasattr(self.model, "deadline"):
            self.model.deadline = min(
                self.model.deadline, time.monotonic() + self.deadline_at - time.time()
            )
        self.pending_action = self._restore_action(self.state.get("action"))
        return tuple(restore_result(v) for v in self.state["results"])

    def resolve_approval(self, task, results, workspace, *, after_business_commit=None):
        if not results or results[-1].status != "approval_required":
            return task, results
        if workspace is None:
            raise CheckpointConflict("approval_executor_required")
        try:
            task = self.store.resume(
                self.task_id,
                workspace,
                checkpoint_owner=(self.token, self.generation),
                after_business_commit=after_business_commit,
            )
        except Exception:  # noqa: BLE001 - business receipts must reconcile ambiguous dispatch.
            raise CheckpointError(
                "approval_execution_interrupted", outcome_unknown=True
            ) from None
        if task.status is not TaskStatus.RUNNING:
            raise CheckpointError(
                "approval_execution_failed",
                outcome_unknown=task.status is TaskStatus.NEEDS_REVIEW,
            )
        with self.store._engine.begin() as connection:
            lock_task(connection, self.task_id)
            self._owned(connection)
            record = self.store._require_approved(connection, self.task_id)
            execution = self.store._read_execution(connection, self.task_id)
            row = execute(
                connection,
                "SELECT a.attempt_id,a.run_id,a.call_id FROM call_attempts a JOIN approval_calls c ON c.call_id=a.call_id WHERE c.approval_id=? AND a.kind='business' AND a.status='SUCCEEDED' ORDER BY a.attempt_index DESC LIMIT 1",
                (record.request.approval_id.hex,),
            ).fetchone()
            if (
                execution.status != "SUCCEEDED"
                or row is None
                or str(UUID(row[2])) != str(results[-1].call_id)
            ):
                raise CheckpointError(
                    "approval_receipt_not_confirmed", outcome_unknown=True
                )
            value = {
                **json.loads(record.request.intent.canonical_arguments),
                "operation_id": str(record.request.approval_id),
                "ticket_revision": execution.ticket_revision,
            }
            result = ToolResult(
                record.request.intent.tool_name,
                "ok",
                "approved_tool_executed",
                json.dumps(value),
                call_id=UUID(row[2]),
                attempt_id=UUID(row[0]),
                run_id=UUID(row[1]),
            )
        resolved = (*results[:-1], result)
        self.save(task, "after_tool", resolved, self.steps)
        return task, resolved

    @staticmethod
    def _restore_action(value):
        if value is None:
            return None
        from .runtime import FinalAnswer

        if value["kind"] == "tool":
            return ToolRequest(value["name"], value["arguments"])
        if value["kind"] == "final":
            return FinalAnswer(value["text"])
        raise ValueError("Invalid checkpoint action")

    def save(self, task, phase, results, steps, action=None):
        if time.time() >= self.deadline_at:
            raise CheckpointError(
                "checkpoint_deadline_exceeded", outcome_unknown=phase == "after_tool"
            )
        from .runtime import FinalAnswer

        serialized = (
            None
            if action is None
            else (
                {"kind": "final", "text": action.text}
                if isinstance(action, FinalAnswer)
                else {
                    "kind": "tool",
                    "name": action.name,
                    "arguments": action.arguments,
                }
            )
        )
        payload = {
            "version": 1,
            "steps": steps,
            "results": [result_state(r) for r in results],
            "action": serialized,
            "model": None if self.stateless_model else self.model.snapshot(),
            "workspace": self.workspace.snapshot()
            if self.workspace is not None
            else None,
        }
        content = encoded(payload)
        if len(content) > MAX_CHECKPOINT_BYTES:
            raise CheckpointError(
                "checkpoint_byte_limit", outcome_unknown=phase == "after_tool"
            )
        with self.store._engine.begin() as connection:
            lock_task(connection, self.task_id)
            session = self._owned(connection)
            if self.store._require_task(connection, self.task_id) != task:
                raise CheckpointConflict("checkpoint_task_changed")
            sequence = execute(
                connection,
                "SELECT coalesce(max(sequence),0)+1 FROM runtime_checkpoints WHERE task_id=?",
                (self.task_id.hex,),
            ).scalar_one()
            identifier, sha = uuid4(), hashlib.sha256(content).hexdigest()
            execute(
                connection,
                "INSERT INTO runtime_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier.hex,
                    self.task_id.hex,
                    session["checkpoint_id"],
                    sequence,
                    self.generation,
                    active_run(connection, self.task_id),
                    task.revision,
                    phase,
                    steps,
                    sha,
                    len(content),
                    content,
                    now(),
                ),
            )
            execute(
                connection,
                "UPDATE runtime_sessions SET checkpoint_id=? WHERE task_id=?",
                (identifier.hex, self.task_id.hex),
            )
            insert_event(
                connection,
                self.task_id,
                "checkpoint.saved",
                {
                    "checkpoint_id": str(identifier),
                    "generation": self.generation,
                    "phase": phase,
                    "step_count": steps,
                    "sha256": sha,
                    "byte_count": len(content),
                },
            )
        self.steps = steps

    def release(self, state):
        with self.store._engine.begin() as connection:
            lock_task(connection, self.task_id)
            require_claim(connection, self.task_id, allow_settled=True)
            execute(
                connection,
                "UPDATE runtime_sessions SET owner_token=NULL,state=? WHERE task_id=? AND owner_token=? AND generation=?",
                (state, self.task_id.hex, self.token, self.generation),
            )

    @classmethod
    def metadata(cls, store, task_id):
        with store._engine.connect() as connection:
            row = (
                execute(
                    connection,
                    "SELECT schema_version,config_sha256,generation,state,checkpoint_id,deadline_at,max_steps,approval_mode,approval_wait_started_at FROM runtime_sessions WHERE task_id=?",
                    (task_id.hex,),
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            checkpoints = (
                execute(
                    connection,
                    "SELECT checkpoint_id,parent_id,sequence,generation,run_id,task_revision,phase,step_count,sha256,byte_count,created_at FROM runtime_checkpoints WHERE task_id=? ORDER BY sequence",
                    (task_id.hex,),
                )
                .mappings()
                .all()
            )

        def public(record):
            return {
                k: str(UUID(v))
                if k in {"checkpoint_id", "parent_id", "run_id"} and v is not None
                else v
                for k, v in record.items()
            }

        return {
            "task_id": str(task_id),
            **public(row),
            "checkpoints": [public(c) for c in checkpoints],
        }
