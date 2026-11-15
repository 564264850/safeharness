"""Durable invocation accounting; this module never dispatches or retries tools."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .contracts import CURRENT_CALL, CallIdentity, InvocationConflictError
from .database import engine_for, execute, lock_task, normalize_database
from .failures import ToolExecutionError


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def active_run(connection, task_id):
    return execute(
        connection,
        "SELECT run_id FROM task_runs WHERE task_id=? AND status='RUNNING' AND kind<>'provider'",
        (task_id.hex,),
    ).scalar_one_or_none()


def ensure_run(connection, task_id, kind="control", *, standalone=False):
    identifier = None if standalone else active_run(connection, task_id)
    if identifier:
        return UUID(identifier)
    run_id = uuid4()
    execute(
        connection,
        "INSERT INTO task_runs VALUES (?,?,?,?,?,NULL)",
        (run_id.hex, task_id.hex, kind, "RUNNING", now()),
    )
    return run_id


def close_runs(connection, task_id, status):
    execute(
        connection,
        "UPDATE task_runs SET status=?,finished_at=? WHERE task_id=? AND status='RUNNING' AND kind<>'provider'",
        (status, now(), task_id.hex),
    )


def new_call(
    connection,
    task_id,
    run_id,
    name,
    kind,
    origin,
    request_sha=None,
    arguments_sha=None,
    version=None,
    policy=None,
):
    ordinal = execute(
        connection,
        "SELECT coalesce(max(ordinal),0)+1 FROM logical_calls WHERE run_id=?",
        (run_id.hex,),
    ).scalar_one()
    call_id = uuid4()
    execute(
        connection,
        "INSERT INTO logical_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (
            call_id.hex,
            task_id.hex,
            run_id.hex,
            ordinal,
            kind,
            origin,
            name[:128],
            version,
            policy,
            request_sha,
            arguments_sha,
            None,
            "OPEN",
            "proposed",
            now(),
        ),
    )
    return call_id


def new_attempt(connection, task_id, run_id, call_id, kind):
    row = execute(
        connection,
        "SELECT task_id,status FROM logical_calls WHERE call_id=?",
        (call_id.hex,),
    ).fetchone()
    if row is None or row[0] != task_id.hex:
        raise InvocationConflictError("Call ownership mismatch")
    index = execute(
        connection,
        "SELECT coalesce(max(attempt_index),0)+1 FROM call_attempts WHERE call_id=?",
        (call_id.hex,),
    ).scalar_one()
    if index > 1 and kind not in {"model", "business"}:
        raise InvocationConflictError("Tool attempts cannot be blindly retried")
    if index > 1 and kind == "model" and row[1] not in {"OPEN", "FAILED", "UNKNOWN"}:
        raise InvocationConflictError("A settled model call cannot be retried")
    if kind == "business" and row[1] not in {"OPEN", "WAITING_APPROVAL"}:
        raise InvocationConflictError("Business call is no longer executable")
    if kind in {"model", "business"}:
        execute(
            connection,
            "UPDATE logical_calls SET status='OPEN',code='attempt_in_progress',finished_at=NULL WHERE call_id=?",
            (call_id.hex,),
        )
    attempt_id = uuid4()
    execute(
        connection,
        "INSERT INTO call_attempts VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (
            attempt_id.hex,
            task_id.hex,
            run_id.hex,
            call_id.hex,
            index,
            kind,
            "INTENT",
            "intent_recorded",
            now(),
        ),
    )
    return CallIdentity(task_id, run_id, call_id, attempt_id, index)


def identity_for_attempt(connection, attempt_id):
    row = execute(
        connection,
        "SELECT task_id,run_id,call_id,attempt_index FROM call_attempts WHERE attempt_id=?",
        (attempt_id.hex,),
    ).fetchone()
    if row is None:
        raise InvocationConflictError("Attempt not found")
    return CallIdentity(UUID(row[0]), UUID(row[1]), UUID(row[2]), attempt_id, row[3])


def put_artifact(
    connection,
    identity,
    value,
    kind="tool_result",
    media_type="text/plain; charset=utf-8",
):
    if value is None:
        return
    content = value.encode("utf-8")
    if len(content) > 262144:
        raise InvocationConflictError("Artifact byte limit exceeded")
    sha = hashlib.sha256(content).hexdigest()
    old = execute(
        connection,
        "SELECT sha256,byte_count FROM call_artifacts WHERE attempt_id=? AND kind=?",
        (identity.attempt_id.hex, kind),
    ).fetchone()
    if old:
        if tuple(old) != (sha, len(content)):
            raise InvocationConflictError("A settled artifact cannot change")
        return
    execute(
        connection,
        "INSERT INTO call_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            uuid4().hex,
            identity.task_id.hex,
            identity.run_id.hex,
            identity.call_id.hex,
            identity.attempt_id.hex,
            kind,
            media_type,
            sha,
            len(content),
            content,
            now(),
        ),
    )


def finish_attempt(
    connection,
    identity,
    status,
    code,
    value=None,
    *,
    settle_call=True,
    artifact_kind="tool_result",
):
    # Local import avoids the scheduling -> Trace/accounting initialization cycle.
    from .scheduling import require_claim

    require_claim(connection, identity.task_id)
    row = execute(
        connection,
        "SELECT status,code FROM call_attempts WHERE attempt_id=? AND task_id=? AND call_id=? AND run_id=?",
        (
            identity.attempt_id.hex,
            identity.task_id.hex,
            identity.call_id.hex,
            identity.run_id.hex,
        ),
    ).fetchone()
    if row is None:
        raise InvocationConflictError("Attempt identity mismatch")
    if row[0] != "INTENT" and tuple(row) != (status, code):
        raise InvocationConflictError("A settled attempt cannot change")
    put_artifact(connection, identity, value, artifact_kind)
    if row[0] == "INTENT":
        execute(
            connection,
            "UPDATE call_attempts SET status=?,code=?,finished_at=? WHERE attempt_id=?",
            (status, code, now(), identity.attempt_id.hex),
        )
        if settle_call:
            call_status = {"REUSED": "SUCCEEDED", "RECONCILED": "SUCCEEDED"}.get(
                status, status
            )
            execute(
                connection,
                "UPDATE logical_calls SET status=?,code=?,finished_at=? WHERE call_id=?",
                (
                    call_status,
                    code,
                    None if call_status == "WAITING_APPROVAL" else now(),
                    identity.call_id.hex,
                ),
            )


def bind_policy(connection, identity, tool, version, policy, arguments_sha, decision):
    if identity_for_attempt(connection, identity.attempt_id) != identity:
        raise InvocationConflictError("Attempt identity mismatch")
    state = execute(
        connection,
        "SELECT status FROM call_attempts WHERE attempt_id=?",
        (identity.attempt_id.hex,),
    ).scalar_one_or_none()
    if state != "INTENT":
        raise InvocationConflictError("A settled attempt cannot dispatch again")
    row = execute(
        connection,
        "SELECT tool_name,arguments_sha256,tool_version,policy_version FROM logical_calls WHERE call_id=? AND task_id=?",
        (identity.call_id.hex, identity.task_id.hex),
    ).fetchone()
    if row is None or row[0] != tool[:128]:
        raise InvocationConflictError("Policy call identity mismatch")
    if row[2] is not None and tuple(row[1:]) != (arguments_sha, version, policy):
        raise InvocationConflictError("Policy binding cannot change")
    execute(
        connection,
        "UPDATE logical_calls SET arguments_sha256=?,tool_version=?,policy_version=? WHERE call_id=?",
        (arguments_sha, version, policy, identity.call_id.hex),
    )
    if decision == "REQUIRE_APPROVAL":
        execute(
            connection,
            "UPDATE call_attempts SET kind='proposal' WHERE attempt_id=?",
            (identity.attempt_id.hex,),
        )


def tool_status(status, code=None):
    if code in {"tool_outcome_unknown", "container_cleanup_failed"}:
        return "UNKNOWN"
    return {
        "ok": "SUCCEEDED",
        "denied": "DENIED",
        "invalid": "DENIED",
        "not_found": "FAILED",
        "approval_required": "WAITING_APPROVAL",
        "failed": "FAILED",
    }[status]


def link_approval(connection, task_id, approval_id, intent, call_id=None):
    if call_id is None:
        run_id = ensure_run(connection, task_id)
        call_id = new_call(
            connection,
            task_id,
            run_id,
            intent.tool_name,
            "tool",
            "approval_import",
            arguments_sha=intent.arguments_sha256,
            version=intent.tool_version,
            policy="mock-ticket-v1",
        )
        execute(
            connection,
            "UPDATE logical_calls SET status='WAITING_APPROVAL',code='approval_imported' WHERE call_id=?",
            (call_id.hex,),
        )
    else:
        unbound = execute(
            connection,
            "SELECT tool_version,request_sha256 FROM logical_calls WHERE call_id=? AND task_id=?",
            (call_id.hex, task_id.hex),
        ).fetchone()
        if unbound and unbound[0] is None:
            if unbound[1] != digest(
                {
                    "tool": intent.tool_name,
                    "arguments": json.loads(intent.canonical_arguments),
                }
            ):
                raise InvocationConflictError("Unbound approval request mismatch")
            execute(
                connection,
                "UPDATE logical_calls SET tool_version=?,policy_version=?,arguments_sha256=? WHERE call_id=?",
                (
                    intent.tool_version,
                    "mock-ticket-v1",
                    intent.arguments_sha256,
                    call_id.hex,
                ),
            )
    row = execute(
        connection,
        "SELECT task_id,tool_name,tool_version,arguments_sha256,status,operation_id FROM logical_calls WHERE call_id=?",
        (call_id.hex,),
    ).fetchone()
    if (
        row is None
        or tuple(row[:5])
        != (
            task_id.hex,
            intent.tool_name,
            intent.tool_version,
            intent.arguments_sha256,
            "WAITING_APPROVAL",
        )
        or row[5] is not None
    ):
        raise InvocationConflictError("Approval does not match its logical call")
    execute(
        connection,
        "INSERT INTO approval_calls VALUES (?,?,?)",
        (approval_id.hex, task_id.hex, call_id.hex),
    )
    execute(
        connection,
        "UPDATE logical_calls SET operation_id=? WHERE call_id=?",
        (approval_id.hex, call_id.hex),
    )
    return call_id


def approval_call(connection, task_id, approval_id, intent):
    value = execute(
        connection,
        "SELECT call_id FROM approval_calls WHERE approval_id=? AND task_id=?",
        (approval_id.hex, task_id.hex),
    ).scalar_one_or_none()
    return (
        UUID(value)
        if value
        else link_approval(connection, task_id, approval_id, intent)
    )


class InvocationLedger:
    def __init__(self, database, task_id):
        self.database = normalize_database(database)
        self.engine = engine_for(self.database)
        self.task_id = task_id

    def active_run_id(self):
        with self.engine.connect() as connection:
            value = active_run(connection, self.task_id)
        return UUID(value) if value else None

    def reusable_model_response(self, model, origin, request_sha):
        """Return a confirmed, integrity-checked response for an identical prompt."""
        from .scheduling import CURRENT_CLAIM, require_claim

        claim = CURRENT_CLAIM.get()
        if claim is None or claim.recovery_mode != "resume":
            return None
        with self.engine.connect() as connection:
            require_claim(connection, self.task_id)
            row = execute(
                connection,
                "SELECT a.content,a.byte_count,a.sha256 FROM logical_calls c JOIN call_attempts t ON t.call_id=c.call_id JOIN call_artifacts a ON a.attempt_id=t.attempt_id JOIN runtime_checkpoints p ON p.checkpoint_id=? WHERE c.task_id=? AND p.task_id=c.task_id AND c.created_at>=p.created_at AND c.tool_name=? AND c.kind='model' AND c.origin=? AND c.request_sha256=? AND c.status='SUCCEEDED' AND t.status='SUCCEEDED' AND a.kind='model_response' ORDER BY t.finished_at DESC LIMIT 1",
                (claim.checkpoint_id, self.task_id.hex, model, origin, request_sha),
            ).fetchone()
            if (
                row is None
                and execute(
                    connection,
                    "SELECT 1 FROM logical_calls c JOIN runtime_checkpoints p ON p.checkpoint_id=? WHERE c.task_id=? AND p.task_id=c.task_id AND c.kind='model' AND c.created_at>=p.created_at AND p.phase='before_model' LIMIT 1",
                    (claim.checkpoint_id, self.task_id.hex),
                ).first()
            ):
                raise InvocationConflictError(
                    "Recovery prompt does not match confirmed response"
                )
        if row is None:
            return None
        content = bytes(row[0])
        if (
            not content
            or len(content) != row[1]
            or hashlib.sha256(content).hexdigest() != row[2]
        ):
            raise InvocationConflictError("Model response artifact integrity error")
        try:
            response = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvocationConflictError(
                "Model response artifact is invalid"
            ) from None
        if not isinstance(response, dict) or response.get("role") != "assistant":
            raise InvocationConflictError("Model response artifact is invalid")
        return response

    def start(self, name, kind, origin, request_sha=None, *, standalone=False):
        from .scheduling import require_claim

        with self.engine.begin() as connection:
            lock_task(connection, self.task_id)
            require_claim(connection, self.task_id)
            row = execute(
                connection,
                "SELECT status FROM tasks WHERE task_id=?",
                (self.task_id.hex,),
            ).fetchone()
            if row is None or (
                kind != "model" and row[0] not in {"RUNNING", "VERIFYING"}
            ):
                raise InvocationConflictError("Task does not allow this invocation")
            run_id = ensure_run(
                connection,
                self.task_id,
                "provider" if standalone else "control",
                standalone=standalone,
            )
            call_id = new_call(
                connection, self.task_id, run_id, name, kind, origin, request_sha
            )
            return new_attempt(
                connection,
                self.task_id,
                run_id,
                call_id,
                "model" if kind == "model" else kind,
            )

    def finish(
        self,
        identity,
        status,
        code,
        value=None,
        *,
        settle_call=True,
        artifact_kind="tool_result",
    ):
        with self.engine.begin() as connection:
            lock_task(connection, self.task_id)
            if identity.task_id != self.task_id:
                raise InvocationConflictError("Task identity mismatch")
            finish_attempt(
                connection,
                identity,
                status,
                code,
                value,
                settle_call=settle_call,
                artifact_kind=artifact_kind,
            )

    def next_model_attempt(self, identity):
        from .scheduling import require_claim

        with self.engine.begin() as connection:
            lock_task(connection, self.task_id)
            require_claim(connection, self.task_id)
            return new_attempt(
                connection, self.task_id, identity.run_id, identity.call_id, "model"
            )

    def close_provider_run(self, identity, status):
        with self.engine.begin() as connection:
            lock_task(connection, self.task_id)
            execute(
                connection,
                "UPDATE task_runs SET status=?,finished_at=? WHERE run_id=? AND task_id=? AND kind='provider' AND status='RUNNING'",
                (status, now(), identity.run_id.hex, self.task_id.hex),
            )

    def invoke_tool(self, request, callback, validator):
        try:
            identity = self.start(
                request.name,
                "tool",
                "runtime",
                digest({"tool": request.name, "arguments": request.arguments}),
            )
        except Exception:  # noqa: BLE001 - no callback has been dispatched
            raise ToolExecutionError(
                "call_ledger_before_dispatch_error", outcome_unknown=False
            ) from None
        token = CURRENT_CALL.set(identity)
        try:
            try:
                result = callback(request)
            except ToolExecutionError as exc:
                self._record_exception(identity, exc)
                raise
            except Exception:  # noqa: BLE001 - arbitrary callbacks have unknown outcomes
                error = ToolExecutionError("tool_outcome_unknown", outcome_unknown=True)
                self._record_exception(identity, error)
                raise error from None
            if not validator(result, request):
                error = ToolExecutionError("invalid_tool_result", outcome_unknown=True)
                self._record_exception(identity, error)
                raise error
            try:
                self.finish(
                    identity,
                    tool_status(result.status, result.code),
                    result.code,
                    result.value,
                )
            except Exception:  # noqa: BLE001 - dispatched result could not be confirmed
                raise ToolExecutionError(
                    "call_result_not_persisted", outcome_unknown=True
                ) from None
            return replace(
                result,
                call_id=identity.call_id,
                attempt_id=identity.attempt_id,
                run_id=identity.run_id,
            )
        finally:
            CURRENT_CALL.reset(token)

    def _record_exception(self, identity, error):
        with self.engine.begin() as connection:
            lock_task(connection, self.task_id)
            row = execute(
                connection,
                "SELECT status FROM call_attempts WHERE attempt_id=?",
                (identity.attempt_id.hex,),
            ).fetchone()
            if row and row[0] == "INTENT":
                finish_attempt(
                    connection,
                    identity,
                    "UNKNOWN" if error.outcome_unknown else "FAILED",
                    error.code,
                )

    def snapshot(self):
        with self.engine.connect() as connection:
            runs = (
                execute(
                    connection,
                    "SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at,run_id",
                    (self.task_id.hex,),
                )
                .mappings()
                .all()
            )
            calls = (
                execute(
                    connection,
                    "SELECT * FROM logical_calls WHERE task_id=? ORDER BY created_at,run_id,ordinal",
                    (self.task_id.hex,),
                )
                .mappings()
                .all()
            )
            attempts = (
                execute(
                    connection,
                    "SELECT * FROM call_attempts WHERE task_id=? ORDER BY started_at,attempt_id",
                    (self.task_id.hex,),
                )
                .mappings()
                .all()
            )
            artifacts = (
                execute(
                    connection,
                    "SELECT artifact_id,task_id,run_id,call_id,attempt_id,kind,media_type,sha256,byte_count,created_at FROM call_artifacts WHERE task_id=? ORDER BY created_at,artifact_id",
                    (self.task_id.hex,),
                )
                .mappings()
                .all()
            )

        def public(row):
            return {
                key: str(UUID(value))
                if value is not None and key.endswith("_id")
                else value
                for key, value in row.items()
            }

        return {
            "task_id": str(self.task_id),
            "runs": [public(row) for row in runs],
            "calls": [public(row) for row in calls],
            "attempts": [public(row) for row in attempts],
            "artifacts": [public(row) for row in artifacts],
        }
