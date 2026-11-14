"""Bounded, database-time recovery decisions. Unconfirmed calls are never replayed."""

import hashlib
import json
from threading import Event
from uuid import UUID, uuid4

from .database import execute
from .invocations import now
from .scheduling import QueueClaim, canonical_request, claim_scope, clock_sql
from .task import Task, TaskStatus
from .trace import insert_event


def classify(connection, task, queue, max_recoveries):
    def decision(mode, code, checkpoint=None, generation=None):
        return mode, code, checkpoint, generation

    try:
        raw = execute(
            connection,
            "SELECT request_json FROM service_tasks WHERE task_id=?",
            (task.task_id.hex,),
        ).scalar_one()
        request = canonical_request(json.loads(raw))
        if (
            request.get("execution_mode") != "queued"
            or request.get("scenario")
            not in {"ticket_read", "ticket_update", "ticket_update_read"}
            or request.get("provider", "scripted") != "scripted"
        ):
            raise ValueError("Unsupported recovery request")
    except (ValueError, TypeError, RecursionError):
        return decision("review", "unsupported_recovery_request")
    if request.get("scenario") in {"ticket_update", "ticket_update_read"}:
        pending = approved_continuation(connection, task, queue, max_recoveries)
        if pending is not None:
            return pending
    if execute(
        connection,
        "SELECT 1 FROM call_attempts WHERE task_id=? AND status IN ('INTENT','UNKNOWN') LIMIT 1",
        (task.task_id.hex,),
    ).first():
        return decision("review", "unconfirmed_call")
    if execute(
        connection,
        "SELECT 1 FROM tool_executions e JOIN pending_approvals a ON e.operation_id=a.approval_id WHERE a.task_id=? AND e.status IN ('INTENT','UNKNOWN') LIMIT 1",
        (task.task_id.hex,),
    ).first():
        return decision("review", "unconfirmed_business_operation")
    if queue["recovery_count"] >= max_recoveries:
        return decision("review", "recovery_limit_reached")
    if queue["execution_deadline_at"] is None:
        return decision("review", "missing_execution_deadline")
    current_time = float(
        execute(connection, "SELECT " + clock_sql(connection)).scalar_one()
    )
    session = (
        execute(
            connection,
            "SELECT * FROM runtime_sessions WHERE task_id=?",
            (task.task_id.hex,),
        )
        .mappings()
        .one_or_none()
    )
    if current_time >= queue["execution_deadline_at"] or (
        session and current_time >= session["deadline_at"]
    ):
        return decision("failed", "execution_deadline_exceeded")
    calls = (
        execute(
            connection,
            "SELECT call_id FROM logical_calls WHERE task_id=?",
            (task.task_id.hex,),
        )
        .scalars()
        .all()
    )
    if task.status is TaskStatus.CREATED:
        return (
            decision("start", "safe_before_start")
            if not session and not calls
            else decision("review", "inconsistent_created_task")
        )
    if session is None:
        return (
            decision("initialize", "safe_before_session")
            if task.status is TaskStatus.RUNNING and not calls
            else decision("review", "missing_runtime_session")
        )
    checkpoint = (
        execute(
            connection,
            "SELECT * FROM runtime_checkpoints WHERE checkpoint_id=? AND task_id=?",
            (session["checkpoint_id"], task.task_id.hex),
        )
        .mappings()
        .one_or_none()
    )
    if checkpoint is None:
        return decision("review", "missing_checkpoint")
    try:
        content = bytes(checkpoint["content"])
        if (
            not 0 < len(content) <= 2097152
            or len(content) != checkpoint["byte_count"]
            or hashlib.sha256(content).hexdigest() != checkpoint["sha256"]
        ):
            raise ValueError("Checkpoint integrity")
        payload = json.loads(content)
        if (
            session["schema_version"] != 1
            or not 0 < session["generation"] <= queue["generation"]
            or checkpoint["generation"] > session["generation"]
        ):
            raise ValueError("Checkpoint ownership")
        if (
            type(payload["version"]) is not int
            or payload["version"] != 1
            or type(payload["steps"]) is not int
            or payload["steps"] != checkpoint["step_count"]
            or not 0 <= payload["steps"] <= session["max_steps"]
        ):
            raise ValueError("Checkpoint steps")
        if (
            payload["model"] is not None
            or payload["workspace"] is not None
            or not isinstance(payload["results"], list)
        ):
            raise ValueError("Unsupported checkpoint adapter")
        recorded = [r["call_id"].replace("-", "") for r in payload["results"]]
        if len(recorded) != len(set(recorded)) or set(recorded) != set(calls):
            return decision("review", "checkpoint_call_evidence_mismatch")
        phase = checkpoint["phase"]
        if phase not in {
            "initial",
            "before_model",
            "after_model",
            "after_tool",
            "final",
        }:
            return decision("review", "unsafe_checkpoint_phase")
        action = payload["action"]
        if phase == "after_model":
            allowed = [
                {
                    "kind": "tool",
                    "name": "read_ticket",
                    "arguments": {"ticket_id": request.get("ticket_id", "IT-001")},
                }
            ]
            if request.get("scenario") in {"ticket_update", "ticket_update_read"}:
                allowed.append(
                    {
                        "kind": "tool",
                        "name": "update_ticket_status",
                        "arguments": {
                            "ticket_id": request.get("ticket_id", "IT-001"),
                            "status": request.get("status", "resolved"),
                        },
                    }
                )
            if action not in allowed:
                raise ValueError("Unexpected pending action")
        elif phase == "final":
            if (
                not isinstance(action, dict)
                or action.get("kind") != "final"
                or not isinstance(action.get("text"), str)
                or not action["text"].strip()
            ):
                raise ValueError("Missing final action")
        elif action is not None:
            raise ValueError("Unexpected action")
        if task.status is TaskStatus.VERIFYING and phase != "final":
            raise ValueError("Verification checkpoint")
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
    ):
        return decision("review", "invalid_checkpoint")
    return decision(
        "resume", "safe_checkpoint", checkpoint["checkpoint_id"], session["generation"]
    )


def scan_one(store, *, max_recoveries=3):
    with store._engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        clock = clock_sql(connection)
        query = f"SELECT t.task_id,t.status,t.revision FROM tasks t JOIN task_queue q ON t.task_id=q.task_id WHERE q.state='CLAIMED' AND q.lease_expires_at<={clock} AND (t.status!='WAITING_APPROVAL' OR q.recovery_mode='approval') ORDER BY q.lease_expires_at,q.task_id LIMIT 1"
        if connection.dialect.name == "postgresql":
            query += " FOR UPDATE OF t SKIP LOCKED"
        row = execute(connection, query).fetchone()
        if row is None:
            return None
        task = Task(UUID(row[0]), TaskStatus(row[1]), row[2])
        query = f"SELECT *,lease_expires_at>{clock} AS lease_valid FROM task_queue WHERE task_id=?"
        if connection.dialect.name == "postgresql":
            query += " FOR UPDATE"
        queue = execute(connection, query, (task.task_id.hex,)).mappings().one()
        if queue["state"] != "CLAIMED" or queue["lease_valid"]:
            return {
                "task_id": str(task.task_id),
                "action": "skipped",
                "code": "lease_renewed",
            }
        terminal = task.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.NEEDS_REVIEW,
        }
        mode, code, checkpoint, generation = (
            ("terminal", "terminal_reconciled", None, None)
            if terminal
            else classify(connection, task, queue, max_recoveries)
        )
        count = queue["recovery_count"] + 1
        execute(
            connection,
            "UPDATE task_queue SET recovery_count=?,recovery_mode=?,recovery_code=?,recovery_checkpoint_id=?,resume_generation=? WHERE task_id=?",
            (count, mode, code, checkpoint, generation, task.task_id.hex),
        )
        if mode in {"start", "initialize", "resume", "approval"}:
            execute(
                connection,
                "UPDATE task_queue SET state='READY' WHERE task_id=?",
                (task.task_id.hex,),
            )
            execute(
                connection,
                "UPDATE tasks SET revision=revision+1 WHERE task_id=?",
                (task.task_id.hex,),
            )
        else:
            claim = QueueClaim(
                task.task_id, "recovery-scanner", queue["generation"] + 1, uuid4().hex
            )
            execute(
                connection,
                f"UPDATE task_queue SET claim_token=?,generation=?,claimed_by=?,claimed_at=?,last_heartbeat_at={clock},lease_expires_at={clock}+30,lease_seconds=30,heartbeat_count=0 WHERE task_id=?",
                (
                    claim.token,
                    claim.generation,
                    claim.worker_id,
                    now(),
                    task.task_id.hex,
                ),
            )
            if terminal:
                execute(
                    connection,
                    "UPDATE task_queue SET state=?,finished_at=? WHERE task_id=?",
                    (
                        "CANCELLED"
                        if task.status is TaskStatus.CANCELLED
                        else "FINISHED",
                        now(),
                        task.task_id.hex,
                    ),
                )
            else:
                with claim_scope(claim):
                    if task.status in {TaskStatus.CREATED, TaskStatus.WAITING_APPROVAL}:
                        running = task.transition(TaskStatus.RUNNING)
                        store._update_task(connection, task, running)
                        task = running
                    updated = task.transition(
                        TaskStatus.FAILED
                        if mode == "failed"
                        else TaskStatus.NEEDS_REVIEW
                    )
                    store._update_task(connection, task, updated)
            execute(
                connection,
                "UPDATE runtime_sessions SET owner_token=NULL,state=? WHERE task_id=?",
                (
                    "NEEDS_REVIEW"
                    if mode == "review" or task.status is TaskStatus.NEEDS_REVIEW
                    else "FINISHED",
                    task.task_id.hex,
                ),
            )
        insert_event(
            connection,
            task.task_id,
            "scheduling.recovery",
            {
                "action": mode,
                "code": code,
                "generation": queue["generation"],
                "recovery_count": count,
            },
        )
        return {
            "task_id": str(task.task_id),
            "action": mode,
            "code": code,
            "recovery_count": count,
        }


def approved_continuation(connection, task, queue, maximum):
    row = execute(
        connection,
        "SELECT s.generation,s.checkpoint_id,s.deadline_at,c.phase,c.content,c.sha256,c.byte_count FROM runtime_sessions s JOIN runtime_checkpoints c ON c.checkpoint_id=s.checkpoint_id AND c.task_id=s.task_id WHERE s.task_id=?",
        (task.task_id.hex,),
    ).fetchone()
    if not row or row[3] != "after_tool":
        return None
    try:
        content = bytes(row[4])
        if (
            len(content) != row[6]
            or len(content) > 2097152
            or hashlib.sha256(content).hexdigest() != row[5]
        ):
            return None
        payload = json.loads(content)
        last = payload["results"][-1]
        if last["status"] != "approval_required":
            return None
        approval = execute(
            connection,
            "SELECT a.approval_id,a.tool_name,a.tool_version,a.canonical_arguments,b.call_id FROM pending_approvals a JOIN approval_decisions d ON d.approval_id=a.approval_id JOIN approval_calls b ON b.approval_id=a.approval_id WHERE a.task_id=? AND d.outcome='APPROVED' ORDER BY a.sequence DESC LIMIT 1",
            (task.task_id.hex,),
        ).fetchone()
        if (
            not approval
            or last["call_id"].replace("-", "") != approval[4]
            or last["approval"]
            != {
                "tool_name": approval[1],
                "tool_version": approval[2],
                "canonical_arguments": approval[3],
            }
        ):
            return None
        other = execute(
            connection,
            "SELECT 1 FROM call_attempts WHERE task_id=? AND status IN ('INTENT','UNKNOWN') AND (call_id!=? OR kind!='business') LIMIT 1",
            (task.task_id.hex, approval[4]),
        ).first()
        if other:
            return ("review", "unconfirmed_call", None, None)
        if queue["recovery_count"] >= maximum:
            return ("review", "recovery_limit_reached", None, None)
        current = float(
            execute(connection, "SELECT " + clock_sql(connection)).scalar_one()
        )
        if queue["execution_deadline_at"] is None or current >= min(
            queue["execution_deadline_at"], row[2]
        ):
            return ("review", "approval_recovery_deadline", None, None)
        return ("approval", "approved_receipt_reconciliation", row[1], row[0])
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        RecursionError,
    ):
        return None


def scan_expired(store, *, batch_size=100, max_recoveries=3):
    if (
        type(batch_size) is not int
        or not 1 <= batch_size <= 100
        or type(max_recoveries) is not int
        or not 1 <= max_recoveries <= 10
    ):
        raise ValueError("Invalid recovery batch or retry limit")
    decisions = []
    for _ in range(batch_size):
        decision = scan_one(store, max_recoveries=max_recoveries)
        if decision is None:
            break
        decisions.append(decision)
    return {"decisions": decisions, "scanned": len(decisions)}


def run_recovery(store, *, watch=False, interval=1, batch_size=100, max_recoveries=3):
    if not 0.05 <= interval <= 30:
        raise ValueError(
            "Recovery polling interval must be between 0.05 and 30 seconds"
        )
    stopped = Event()
    try:
        while True:
            report = scan_expired(
                store, batch_size=batch_size, max_recoveries=max_recoveries
            )
            if report["scanned"] or not watch:
                print(json.dumps(report), flush=True)
            if not watch:
                return 0
            stopped.wait(interval)
    except KeyboardInterrupt:
        return 0
