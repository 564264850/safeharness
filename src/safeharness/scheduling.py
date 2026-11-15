"""Durable queue, database-time leases and generation-bound execution ownership."""

import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from uuid import UUID, uuid4

from .database import execute
from .invocations import now
from .trace import insert_event


class QueueClaimError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueueClaim:
    task_id: UUID
    worker_id: str
    generation: int
    token: str = field(repr=False)
    lease_seconds: float = 30.0
    fenced: Event = field(default_factory=Event, repr=False, compare=False)
    recovery_mode: str | None = None
    checkpoint_id: str | None = None
    resume_generation: int | None = None


CURRENT_CLAIM = ContextVar("queue_claim", default=None)


@contextmanager
def claim_scope(claim):
    token = CURRENT_CLAIM.set(claim)
    try:
        yield
    finally:
        CURRENT_CLAIM.reset(token)


def clock_sql(connection):
    return (
        "EXTRACT(EPOCH FROM clock_timestamp())"
        if connection.dialect.name == "postgresql"
        else "(julianday('now')-2440587.5)*86400.0"
    )


def valid_lease_seconds(value):
    if (
        type(value) not in (float, int)
        or not math.isfinite(value)
        or not 0.5 <= value <= 3600
    ):
        raise ValueError("Lease duration must be between 0.5 and 3600 seconds")
    return float(value)


def require_claim(connection, task_id, *, allow_settled=False):
    row = execute(
        connection,
        f"SELECT state,claim_token,generation,lease_expires_at>{clock_sql(connection)},recovery_mode FROM task_queue WHERE task_id=?",
        (task_id.hex,),
    ).fetchone()
    if row is None:
        return None
    claim = CURRENT_CLAIM.get()
    if (
        claim is None
        or claim.task_id != task_id
        or tuple(row[1:3]) != (claim.token, claim.generation)
    ):
        raise QueueClaimError("Queued tasks require a current worker claim")
    if allow_settled and (
        row[0] in {"FINISHED", "CANCELLED", "WAITING"}
        or (row[0] == "READY" and row[4] == "approval")
    ):
        return claim
    if row[0] != "CLAIMED":
        raise QueueClaimError("Queued tasks require a current worker claim")
    if claim.fenced.is_set():
        raise QueueClaimError("queue_heartbeat_lost")
    if not row[3]:
        raise QueueClaimError("queue_lease_expired")
    return claim


def claim_next(store, worker_id, *, lease_seconds=30):
    lease_seconds = valid_lease_seconds(lease_seconds)
    if not isinstance(worker_id, str) or not re.fullmatch(
        r"[A-Za-z0-9._-]{1,80}", worker_id
    ):
        raise ValueError("Invalid worker ID")
    with store._engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        query = (
            "SELECT t.task_id,q.generation,q.recovery_mode,q.recovery_checkpoint_id,q.resume_generation FROM tasks t JOIN task_queue q ON t.task_id=q.task_id "
            "WHERE (q.state='QUEUED' AND t.status='CREATED') OR (q.state='READY' AND t.status IN ('CREATED','RUNNING','VERIFYING','WAITING_APPROVAL')) "
            "ORDER BY q.created_at,q.task_id LIMIT 1"
        )
        if connection.dialect.name == "postgresql":
            query += " FOR UPDATE OF t SKIP LOCKED"
        row = execute(connection, query).fetchone()
        if row is None:
            return None
        # A join can retain an old queue snapshot while locking an unchanged task row.
        # Re-read queue ownership after that lock, also serializing heartbeat updates.
        fresh_query = "SELECT generation,recovery_mode,recovery_checkpoint_id,resume_generation,state FROM task_queue WHERE task_id=?"
        if connection.dialect.name == "postgresql":
            fresh_query += " FOR UPDATE"
        fresh = execute(connection, fresh_query, (row[0],)).fetchone()
        if fresh[4] not in {"QUEUED", "READY"}:
            return None
        claim = QueueClaim(
            UUID(row[0]),
            worker_id,
            fresh[0] + 1,
            uuid4().hex,
            lease_seconds,
            recovery_mode=fresh[1],
            checkpoint_id=fresh[2],
            resume_generation=fresh[3],
        )
        clock = clock_sql(connection)
        changed = execute(
            connection,
            f"UPDATE task_queue SET state='CLAIMED',claim_token=?,claimed_by=?,generation=?,claimed_at=?,lease_seconds=?,last_heartbeat_at={clock},lease_expires_at={clock}+?,heartbeat_count=0,finished_at=NULL,execution_deadline_at=coalesce(execution_deadline_at,{clock}+120) "
            "WHERE task_id=? AND state IN ('QUEUED','READY')",
            (
                claim.token,
                worker_id,
                claim.generation,
                now(),
                lease_seconds,
                lease_seconds,
                claim.task_id.hex,
            ),
        )
        if changed.rowcount != 1:
            raise QueueClaimError("Queue claim changed")
        insert_event(
            connection,
            claim.task_id,
            "scheduling.claimed",
            {"worker_id": worker_id, "generation": claim.generation},
        )
    return claim


def renew_claim(store, claim, *, engine=None):
    if claim.fenced.is_set():
        raise QueueClaimError("queue_heartbeat_lost")
    with (engine or store._engine).begin() as connection:
        clock = clock_sql(connection)
        changed = execute(
            connection,
            f"UPDATE task_queue SET last_heartbeat_at={clock},lease_expires_at={clock}+lease_seconds,heartbeat_count=heartbeat_count+1 "
            f"WHERE task_id=? AND state='CLAIMED' AND claim_token=? AND generation=? AND lease_expires_at>{clock}",
            (claim.task_id.hex, claim.token, claim.generation),
        )
        if claim.fenced.is_set():
            raise QueueClaimError("queue_heartbeat_lost")
        if changed.rowcount == 1:
            return True
        row = execute(
            connection,
            "SELECT state,claim_token,generation,recovery_mode FROM task_queue WHERE task_id=?",
            (claim.task_id.hex,),
        ).fetchone()
        if (
            row
            and (
                row[0] in {"FINISHED", "CANCELLED", "WAITING"}
                or (row[0] == "READY" and row[3] == "approval")
            )
            and tuple(row[1:3]) == (claim.token, claim.generation)
        ):
            return False
        raise QueueClaimError("queue_lease_expired_or_changed")


def finish_claim(connection, task_id, outcome):
    claim = require_claim(connection, task_id)
    if claim is not None:
        execute(
            connection,
            "UPDATE task_queue SET state='FINISHED',finished_at=? WHERE task_id=?",
            (now(), task_id.hex),
        )
        insert_event(
            connection,
            task_id,
            "scheduling.finished",
            {
                "worker_id": claim.worker_id,
                "generation": claim.generation,
                "status": outcome,
            },
        )


def public_scheduling(row):
    if row is None or row["state"] is None:
        return None
    result = {
        "mode": "queued",
        **{k: row[k] for k in ("schema_version", "state", "created_at")},
    }
    for key in ("generation", "claimed_by", "claimed_at", "finished_at"):
        if row[key]:
            result[key] = row[key]
    for key in (
        "recovery_count",
        "recovery_mode",
        "recovery_code",
        "execution_deadline_at",
    ):
        if row[key]:
            result[key] = row[key]
    if row["lease_seconds"] is not None:
        for key in (
            "lease_seconds",
            "last_heartbeat_at",
            "lease_expires_at",
            "heartbeat_count",
        ):
            result[key] = row[key]
        result["lease_status"] = (
            ("ACTIVE" if row["lease_valid"] else "EXPIRED")
            if row["state"] == "CLAIMED"
            else "RELEASED"
        )
    return result


def canonical_request(request):
    """Preserve pre-queue idempotency digests for default/manual submissions."""
    request = dict(request)
    mode = request.get("execution_mode", "manual")
    if mode == "manual":
        request.pop("execution_mode", None)
    elif mode != "queued" or (
        request.get("scenario")
        not in {"ticket_read", "ticket_update", "ticket_update_read"}
        or request.get("provider", "scripted") not in ("scripted", "deepseek")
    ):
        raise ValueError(
            "Queued mode supports ticket scenarios and known providers only"
        )
    return request


def enqueue(connection, task_id):
    execute(
        connection,
        "INSERT INTO task_queue (task_id,schema_version,state,created_at) VALUES (?,1,'QUEUED',?)",
        (task_id.hex, now()),
    )
    insert_event(connection, task_id, "scheduling.enqueued", {"schema_version": 1})


def scheduling(connection, task_id):
    row = (
        execute(
            connection,
            f"SELECT schema_version,state,created_at,generation,claimed_by,claimed_at,finished_at,lease_seconds,last_heartbeat_at,lease_expires_at,heartbeat_count,recovery_count,recovery_mode,recovery_code,execution_deadline_at,lease_expires_at>{clock_sql(connection)} AS lease_valid FROM task_queue WHERE task_id=?",
            (task_id.hex,),
        )
        .mappings()
        .one_or_none()
    )
    return public_scheduling(row)


def cancel_queued(connection, task_id):
    result = execute(
        connection,
        "UPDATE task_queue SET state='CANCELLED',finished_at=? WHERE task_id=? AND state IN ('QUEUED','READY','WAITING','CLAIMED')",
        (now(), task_id.hex),
    )
    if result.rowcount:
        insert_event(connection, task_id, "scheduling.cancelled", {"schema_version": 1})


def wake_approval(connection, task_id):
    row = execute(
        connection,
        "SELECT checkpoint_id,generation,approval_wait_started_at FROM runtime_sessions WHERE task_id=?",
        (task_id.hex,),
    ).fetchone()
    if row is None:
        return
    clock = clock_sql(connection)
    paused = (
        max(0, float(execute(connection, "SELECT " + clock).scalar_one()) - row[2])
        if row[2] is not None
        else 0.0
    )
    changed = execute(
        connection,
        "UPDATE task_queue SET state='READY',recovery_mode='approval',recovery_code='approval_granted',recovery_checkpoint_id=?,resume_generation=?,execution_deadline_at=execution_deadline_at+? WHERE task_id=? AND state='WAITING'",
        (row[0], row[1], paused, task_id.hex),
    )
    if changed.rowcount:
        execute(
            connection,
            "UPDATE runtime_sessions SET deadline_at=deadline_at+?,approval_wait_started_at=NULL WHERE task_id=?",
            (paused, task_id.hex),
        )
        insert_event(
            connection, task_id, "scheduling.approval_ready", {"generation": row[1]}
        )
